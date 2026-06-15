import json
from typing import Any

from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.conf import settings

from apps.organizations.scoping import get_org_context
from apps.rmct.collab_locks import mem_get_lock, mem_release_user_keys, mem_try_lock, mem_unlock
from apps.rmct.page_edit_locks import (
    PAGE_EDIT_TTL_SECONDS,
    mem_get_page_edit,
    mem_release_page_edit,
    mem_release_user_page_edits,
    mem_try_page_edit,
    page_edit_lock_key,
)
from apps.rmct.collab_service import apply_collab_cell, apply_operation_cell
from apps.rmct.models import RMCMModel
from redis.asyncio import Redis


_redis: Redis | None = None


def _redis_client() -> Redis:
    global _redis
    if _redis is None:
        from RMCT.redis_config import async_redis_kwargs, redis_url

        _redis = Redis.from_url(redis_url(), **async_redis_kwargs())
    return _redis


def _redis_enabled() -> bool:
    # If using in-memory channel layer (no Redis), disable redis-backed locks/presence.
    return not bool(getattr(settings, "USE_INMEMORY_CHANNEL_LAYER", False))


def _group_name(model_id: str) -> str:
    return f"model_{model_id}"


def _org_group_name(org_id: str) -> str:
    return f"org_{org_id}"


def _lock_key(*, org_id: str, model_id: str, entity: str, row_id: str, column: str) -> str:
    return f"lock:{org_id}:{model_id}:{entity}:{row_id}:{column}"


def _presence_key(*, org_id: str) -> str:
    return f"presence:org:{org_id}"


def _model_presence_key(*, model_id: str) -> str:
    return f"presence:{model_id}"


def _user_locks_key(*, org_id: str, user_id: int) -> str:
    return f"userlocks:org:{org_id}:{user_id}"


def _user_page_edits_key(*, org_id: str, user_id: int) -> str:
    return f"userpageedits:org:{org_id}:{user_id}"


class ModelCollaborationConsumer(AsyncWebsocketConsumer):
    """
    Websocket room: model_{model_id}

    Supported inbound events:
    - lock_cell { row_id, column }
    - unlock_cell { row_id, column }
    - update_cell { row_id, column, value }  (Operation fields for now)
    - heartbeat {}
    """

    async def connect(self):
        user = self.scope.get("user")
        if not user or not getattr(user, "is_authenticated", False):
            await self.close(code=4401)
            return

        self.model_id = str(self.scope["url_route"]["kwargs"]["model_id"])
        self.group = _group_name(self.model_id)

        # Validate org membership and model ownership
        ctx, err = await sync_to_async(get_org_context)(self._fake_request())
        if err is not None:
            await self.close(code=4403)
            return

        model_ok = await sync_to_async(
            lambda: RMCMModel.objects.filter(id=self.model_id, organization_id=ctx.organization.id).exists()
        )()
        if not model_ok:
            await self.close(code=4404)
            return

        self.ctx_org_id = str(ctx.organization.id)
        self.profile_name = ctx.profile.full_name or user.get_full_name() or user.email

        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()

        if _redis_enabled():
            await self._presence_touch_model()
            snapshot = await self._presence_snapshot_model()
            if snapshot:
                await self.send_json({"type": "presence_snapshot", "users": snapshot})
        await self.channel_layer.group_send(
            self.group,
            {
                "type": "broadcast",
                "payload": {"type": "user_joined", "user_id": user.id, "name": self.profile_name},
            },
        )

    async def disconnect(self, close_code):
        if hasattr(self, "group"):
            await self.channel_layer.group_discard(self.group, self.channel_name)
        try:
            if _redis_enabled():
                await self._release_all_user_locks()
                await self._presence_remove()
        except Exception:
            pass
        user = self.scope.get("user")
        if user and getattr(user, "is_authenticated", False) and hasattr(self, "group"):
            await self.channel_layer.group_send(
                self.group,
                {
                    "type": "broadcast",
                    "payload": {"type": "user_left", "user_id": user.id, "name": getattr(self, "profile_name", "")},
                },
            )

    async def receive(self, text_data=None, bytes_data=None):
        if not text_data:
            return
        try:
            msg = json.loads(text_data)
        except Exception:
            return
        msg_type = msg.get("type")

        if msg_type == "heartbeat":
            if _redis_enabled():
                await self._presence_touch()
            await self.send_json({"type": "heartbeat_ack"})
            return

        if msg_type == "lock_cell":
            await self._handle_lock(msg)
            return

        if msg_type == "unlock_cell":
            await self._handle_unlock(msg)
            return

        if msg_type == "update_cell":
            await self._handle_update_cell(msg)
            return

    async def broadcast(self, event: dict[str, Any]):
        payload = event.get("payload") or {}
        await self.send_json(payload)

    async def send_json(self, payload: dict[str, Any]):
        await self.send(text_data=json.dumps(payload))

    def _fake_request(self):
        # get_org_context expects a Django request-like object with user
        class R:
            pass

        r = R()
        r.user = self.scope.get("user")
        return r

    async def _presence_touch_model(self):
        r = _redis_client()
        key = _model_presence_key(model_id=self.model_id)
        user = self.scope["user"]
        try:
            await r.hset(key, mapping={str(user.id): self.profile_name})
            await r.expire(key, 60)
        except Exception:
            return

    async def _presence_snapshot_model(self) -> list[dict[str, Any]] | None:
        r = _redis_client()
        key = _model_presence_key(model_id=self.model_id)
        try:
            raw = await r.hgetall(key)
        except Exception:
            return None
        if not raw:
            return []
        return [{"user_id": int(uid), "name": name} for uid, name in raw.items()]

    async def _presence_remove(self):
        r = _redis_client()
        key = _model_presence_key(model_id=self.model_id)
        user = self.scope["user"]
        try:
            await r.hdel(key, str(user.id))
        except Exception:
            return

    async def _release_all_user_locks(self):
        if not hasattr(self, "model_id"):
            return
        user = self.scope.get("user")
        if not user or not getattr(user, "is_authenticated", False):
            return
        r = _redis_client()
        ukey = _user_locks_key(org_id=self.ctx_org_id, user_id=user.id)
        try:
            keys = await r.smembers(ukey)
        except Exception:
            return
        if not keys:
            return
        for k in keys:
            try:
                locked_by = await r.get(k)
                if locked_by == str(user.id):
                    await r.delete(k)
            except Exception:
                continue
        await r.delete(ukey)

    async def _handle_lock(self, msg: dict[str, Any]):
        row_id = str(msg.get("row_id") or "")
        column = str(msg.get("column") or "")
        if not row_id or not column:
            return

        user = self.scope["user"]
        if not _redis_enabled():
            # Dev fallback: no locks without Redis
            await self.channel_layer.group_send(
                self.group,
                {
                    "type": "broadcast",
                    "payload": {
                        "type": "cell_locked",
                        "row_id": row_id,
                        "column": column,
                        "locked_by": user.id,
                        "name": self.profile_name,
                    },
                },
            )
            return
        r = _redis_client()
        key = _lock_key(
            org_id=self.ctx_org_id,
            model_id=self.model_id,
            entity="operation",
            row_id=row_id,
            column=column,
        )

        # SETNX with TTL
        try:
            ok = await r.set(key, str(user.id), nx=True, ex=30)
        except Exception:
            # Redis down: behave as if locking is best-effort
            ok = True
        if ok:
            # Track locks held by this user so we can release on disconnect.
            try:
                await r.sadd(_user_locks_key(org_id=self.ctx_org_id, user_id=user.id), key)
                await r.expire(_user_locks_key(org_id=self.ctx_org_id, user_id=user.id), 120)
            except Exception:
                pass
            await self.channel_layer.group_send(
                self.group,
                {
                    "type": "broadcast",
                    "payload": {
                        "type": "cell_locked",
                        "row_id": row_id,
                        "column": column,
                        "locked_by": user.id,
                        "name": self.profile_name,
                    },
                },
            )
        else:
            try:
                locked_by = await r.get(key)
            except Exception:
                locked_by = None
            await self.send_json(
                {"type": "lock_denied", "row_id": row_id, "column": column, "locked_by": locked_by}
            )

    async def _handle_unlock(self, msg: dict[str, Any]):
        row_id = str(msg.get("row_id") or "")
        column = str(msg.get("column") or "")
        if not row_id or not column:
            return

        user = self.scope["user"]
        if not _redis_enabled():
            await self.channel_layer.group_send(
                self.group,
                {
                    "type": "broadcast",
                    "payload": {"type": "cell_unlocked", "row_id": row_id, "column": column},
                },
            )
            return
        r = _redis_client()
        key = _lock_key(
            org_id=self.ctx_org_id,
            model_id=self.model_id,
            entity="operation",
            row_id=row_id,
            column=column,
        )
        try:
            locked_by = await r.get(key)
        except Exception:
            locked_by = str(user.id)
        if locked_by and locked_by != str(user.id):
            return
        try:
            await r.delete(key)
            await r.srem(_user_locks_key(org_id=self.ctx_org_id, user_id=user.id), key)
        except Exception:
            pass
        await self.channel_layer.group_send(
            self.group,
            {
                "type": "broadcast",
                "payload": {
                    "type": "cell_unlocked",
                    "row_id": row_id,
                    "column": column,
                },
            },
        )

    async def _handle_update_cell(self, msg: dict[str, Any]):
        row_id = str(msg.get("row_id") or "")
        column = str(msg.get("column") or "")
        value = msg.get("value")
        if not row_id or not column:
            return

        user = self.scope["user"]
        if _redis_enabled():
            r = _redis_client()
            key = _lock_key(
                org_id=self.ctx_org_id,
                model_id=self.model_id,
                entity="operation",
                row_id=row_id,
                column=column,
            )
            try:
                locked_by = await r.get(key)
            except Exception:
                locked_by = None
            if locked_by and locked_by != str(user.id):
                await self.send_json({"type": "update_denied", "reason": "not_lock_owner"})
                return

        result = await sync_to_async(apply_operation_cell)(
            model_id=self.model_id,
            org_id=self.ctx_org_id,
            row_id=row_id,
            column=column,
            value=value,
        )
        if not result:
            await self.send_json({"type": "update_denied", "reason": "row_not_found"})
            return

        payload = {
            "type": "cell_updated",
            **result,
            "updated_by": user.id,
            "name": self.profile_name,
        }
        await self.channel_layer.group_send(
            self.group, {"type": "broadcast", "payload": payload}
        )
        org_group = _org_group_name(self.ctx_org_id)
        await self.channel_layer.group_send(
            org_group, {"type": "broadcast", "payload": payload}
        )


class OrganizationCollaborationConsumer(AsyncWebsocketConsumer):
    """
    Org-wide collaboration room: org_{organization_id}

    One live connection per authenticated org member (connect on app login).
  All org members receive cell updates for any model in the organization.

    Inbound: heartbeat, lock_cell, unlock_cell, update_cell (each requires model_id)
    """

    async def connect(self):
        user = self.scope.get("user")
        if not user or not getattr(user, "is_authenticated", False):
            await self.close(code=4401)
            return

        ctx, err = await sync_to_async(get_org_context)(self._fake_request())
        if err is not None:
            await self.close(code=4403)
            return

        self.ctx_org_id = str(ctx.organization.id)
        self.profile_name = ctx.profile.full_name or user.get_full_name() or user.email
        self.group = _org_group_name(self.ctx_org_id)

        try:
            await self.channel_layer.group_add(self.group, self.channel_name)
        except Exception:
            await self.close(code=1011)
            return

        await self.accept()

        if _redis_enabled():
            try:
                await self._presence_touch()
                snapshot = await self._presence_snapshot()
                await self.send_json(
                    {"type": "presence_snapshot", "users": snapshot or []}
                )
            except Exception:
                pass

        try:
            await self.channel_layer.group_send(
                self.group,
                {
                    "type": "broadcast",
                    "payload": {
                        "type": "user_joined",
                        "user_id": user.id,
                        "name": self.profile_name,
                    },
                },
            )
        except Exception:
            pass

    async def disconnect(self, close_code):
        if hasattr(self, "group"):
            try:
                await self.channel_layer.group_discard(self.group, self.channel_name)
            except Exception:
                pass
        user = self.scope.get("user")
        try:
            if _redis_enabled():
                await self._release_all_user_locks()
                await self._release_all_user_page_edits()
                await self._presence_remove()
            elif user and getattr(user, "is_authenticated", False):
                released = mem_release_user_keys(user_id=user.id)
                for key in released:
                    parts = key.split(":", 5)
                    if len(parts) < 6:
                        continue
                    _, _org, model_id, entity, row_id, column = parts
                    await self.channel_layer.group_send(
                        self.group,
                        {
                            "type": "broadcast",
                            "payload": {
                                "type": "cell_unlocked",
                                "entity": entity,
                                "model_id": model_id,
                                "row_id": row_id,
                                "column": column,
                            },
                        },
                    )
                page_released = mem_release_user_page_edits(user_id=user.id)
                for key in page_released:
                    payload = _page_edit_payload_from_key(key, active=False)
                    if payload:
                        await self.channel_layer.group_send(
                            self.group,
                            {"type": "broadcast", "payload": payload},
                        )
        except Exception:
            pass
        if user and getattr(user, "is_authenticated", False) and hasattr(self, "group"):
            await self.channel_layer.group_send(
                self.group,
                {
                    "type": "broadcast",
                    "payload": {
                        "type": "user_left",
                        "user_id": user.id,
                        "name": getattr(self, "profile_name", ""),
                    },
                },
            )

    async def receive(self, text_data=None, bytes_data=None):
        if not text_data:
            return
        try:
            msg = json.loads(text_data)
        except Exception:
            return
        msg_type = msg.get("type")

        if msg_type == "heartbeat":
            if _redis_enabled():
                await self._presence_touch()
            await self.send_json({"type": "heartbeat_ack"})
            return

        if msg_type == "lock_cell":
            await self._handle_lock(msg)
            return

        if msg_type == "unlock_cell":
            await self._handle_unlock(msg)
            return

        if msg_type == "update_cell":
            await self._handle_update_cell(msg)
            return

        if msg_type == "notify_model_saved":
            await self._handle_notify_model_saved(msg)
            return

        if msg_type == "notify_model_library_changed":
            await self._handle_notify_model_library_changed(msg)
            return

        if msg_type == "acquire_page_edit":
            await self._handle_acquire_page_edit(msg)
            return

        if msg_type == "release_page_edit":
            await self._handle_release_page_edit(msg)
            return

    async def broadcast(self, event: dict[str, Any]):
        await self.send_json(event.get("payload") or {})

    async def send_json(self, payload: dict[str, Any]):
        await self.send(text_data=json.dumps(payload))

    def _fake_request(self):
        class R:
            pass

        r = R()
        r.user = self.scope.get("user")
        return r

    async def _presence_touch(self):
        r = _redis_client()
        key = _presence_key(org_id=self.ctx_org_id)
        user = self.scope["user"]
        try:
            await r.hset(key, mapping={str(user.id): self.profile_name})
            await r.expire(key, 120)
        except Exception:
            return

    async def _presence_snapshot(self) -> list[dict[str, Any]] | None:
        r = _redis_client()
        key = _presence_key(org_id=self.ctx_org_id)
        try:
            raw = await r.hgetall(key)
        except Exception:
            return None
        if not raw:
            return []
        users = []
        for uid, name in raw.items():
            try:
                users.append({"user_id": int(uid), "name": name})
            except ValueError:
                users.append({"user_id": uid, "name": name})
        return users

    async def _presence_remove(self):
        r = _redis_client()
        key = _presence_key(org_id=self.ctx_org_id)
        user = self.scope["user"]
        try:
            await r.hdel(key, str(user.id))
        except Exception:
            return

    async def _release_all_user_locks(self):
        user = self.scope.get("user")
        if not user or not getattr(user, "is_authenticated", False):
            return
        r = _redis_client()
        ukey = _user_locks_key(org_id=self.ctx_org_id, user_id=user.id)
        try:
            keys = await r.smembers(ukey)
        except Exception:
            return
        for k in keys:
            try:
                locked_by = await r.get(k)
                if locked_by == str(user.id):
                    await r.delete(k)
            except Exception:
                continue
        try:
            await r.delete(ukey)
        except Exception:
            pass

    async def _release_all_user_page_edits(self):
        user = self.scope.get("user")
        if not user or not getattr(user, "is_authenticated", False):
            return
        r = _redis_client()
        ukey = _user_page_edits_key(org_id=self.ctx_org_id, user_id=user.id)
        try:
            keys = await r.smembers(ukey)
        except Exception:
            return
        for k in keys:
            try:
                locked_by = await r.get(k)
                if locked_by == str(user.id):
                    await r.delete(k)
                    payload = _page_edit_payload_from_key(k, active=False)
                    if payload:
                        await self.channel_layer.group_send(
                            self.group,
                            {"type": "broadcast", "payload": payload},
                        )
            except Exception:
                continue
        try:
            await r.delete(ukey)
        except Exception:
            pass

    async def _handle_lock(self, msg: dict[str, Any]):
        entity = str(msg.get("entity") or "operation").strip().lower()
        model_id = str(msg.get("model_id") or "")
        row_id = str(msg.get("row_id") or "")
        column = str(msg.get("column") or "")
        if not model_id or not row_id or not column:
            return

        if not await self._model_in_org(model_id):
            return

        user = self.scope["user"]
        payload = {
            "type": "cell_locked",
            "entity": entity,
            "model_id": model_id,
            "row_id": row_id,
            "column": column,
            "locked_by": user.id,
            "name": self.profile_name,
        }

        key = _lock_key(
            org_id=self.ctx_org_id,
            model_id=model_id,
            entity=entity,
            row_id=row_id,
            column=column,
        )

        if not _redis_enabled():
            ok = mem_try_lock(key=key, user_id=str(user.id), name=self.profile_name)
            if ok:
                await self.channel_layer.group_send(
                    self.group, {"type": "broadcast", "payload": payload}
                )
            else:
                holder = mem_get_lock(key)
                await self.send_json(
                    {
                        "type": "lock_denied",
                        "entity": entity,
                        "model_id": model_id,
                        "row_id": row_id,
                        "column": column,
                        "locked_by": holder[0] if holder else None,
                        "name": holder[1] if holder else "Another user",
                    }
                )
            return

        r = _redis_client()
        try:
            ok = await r.set(key, str(user.id), nx=True, ex=45)
        except Exception:
            ok = True
        if ok:
            try:
                await r.sadd(_user_locks_key(org_id=self.ctx_org_id, user_id=user.id), key)
                await r.expire(_user_locks_key(org_id=self.ctx_org_id, user_id=user.id), 120)
            except Exception:
                pass
            await self.channel_layer.group_send(
                self.group, {"type": "broadcast", "payload": payload}
            )
        else:
            try:
                locked_by = await r.get(key)
            except Exception:
                locked_by = None
            holder_name = None
            if locked_by:
                try:
                    holder_name = await r.hget(
                        _presence_key(org_id=self.ctx_org_id), locked_by
                    )
                except Exception:
                    holder_name = None
            await self.send_json(
                {
                    "type": "lock_denied",
                    "entity": entity,
                    "model_id": model_id,
                    "row_id": row_id,
                    "column": column,
                    "locked_by": locked_by,
                    "name": holder_name or "Another user",
                }
            )

    async def _handle_unlock(self, msg: dict[str, Any]):
        entity = str(msg.get("entity") or "operation").strip().lower()
        model_id = str(msg.get("model_id") or "")
        row_id = str(msg.get("row_id") or "")
        column = str(msg.get("column") or "")
        if not model_id or not row_id or not column:
            return

        user = self.scope["user"]
        payload = {
            "type": "cell_unlocked",
            "entity": entity,
            "model_id": model_id,
            "row_id": row_id,
            "column": column,
        }

        key = _lock_key(
            org_id=self.ctx_org_id,
            model_id=model_id,
            entity=entity,
            row_id=row_id,
            column=column,
        )

        if not _redis_enabled():
            mem_unlock(key=key, user_id=str(user.id))
            await self.channel_layer.group_send(
                self.group, {"type": "broadcast", "payload": payload}
            )
            return

        r = _redis_client()
        try:
            locked_by = await r.get(key)
        except Exception:
            locked_by = None
        if locked_by and locked_by != str(user.id):
            return
        try:
            await r.delete(key)
            await r.srem(_user_locks_key(org_id=self.ctx_org_id, user_id=user.id), key)
        except Exception:
            pass
        await self.channel_layer.group_send(
            self.group, {"type": "broadcast", "payload": payload}
        )

    async def _handle_notify_model_saved(self, msg: dict[str, Any]):
        model_id = str(msg.get("model_id") or "")
        if not model_id:
            return
        if not await self._model_in_org(model_id):
            return
        user = self.scope["user"]
        await self.channel_layer.group_send(
            self.group,
            {
                "type": "broadcast",
                "payload": {
                    "type": "model_refreshed",
                    "model_id": model_id,
                    "scope": str(msg.get("scope") or "full"),
                    "updated_by": user.id,
                    "name": self.profile_name,
                },
            },
        )

    async def _handle_notify_model_library_changed(self, msg: dict[str, Any]):
        user = self.scope["user"]
        await self.channel_layer.group_send(
            self.group,
            {
                "type": "broadcast",
                "payload": {
                    "type": "model_library_changed",
                    "updated_by": user.id,
                    "name": self.profile_name,
                },
            },
        )

    async def _handle_update_cell(self, msg: dict[str, Any]):
        entity = str(msg.get("entity") or "operation").strip().lower()
        model_id = str(msg.get("model_id") or "")
        row_id = str(msg.get("row_id") or "")
        column = str(msg.get("column") or "")
        value = msg.get("value")
        if not model_id or not row_id or not column:
            return

        if not await self._model_in_org(model_id):
            await self.send_json({"type": "update_denied", "reason": "model_not_found"})
            return

        user = self.scope["user"]
        key = _lock_key(
            org_id=self.ctx_org_id,
            model_id=model_id,
            entity=entity,
            row_id=row_id,
            column=column,
        )
        if _redis_enabled():
            r = _redis_client()
            try:
                locked_by = await r.get(key)
            except Exception:
                locked_by = None
            if locked_by and locked_by != str(user.id):
                await self.send_json({"type": "update_denied", "reason": "not_lock_owner"})
                return
        else:
            holder = mem_get_lock(key)
            if holder and holder[0] != str(user.id):
                await self.send_json({"type": "update_denied", "reason": "not_lock_owner"})
                return

        result = await sync_to_async(apply_collab_cell)(
            entity=entity,
            model_id=model_id,
            org_id=self.ctx_org_id,
            row_id=row_id,
            column=column,
            value=value,
        )
        if not result:
            await self.send_json({"type": "update_denied", "reason": "row_not_found"})
            return

        payload = {
            "type": "cell_updated",
            **result,
            "updated_by": user.id,
            "name": self.profile_name,
        }
        await self.channel_layer.group_send(
            self.group, {"type": "broadcast", "payload": payload}
        )

    async def _model_in_org(self, model_id: str) -> bool:
        return await sync_to_async(
            lambda: RMCMModel.objects.filter(
                id=model_id, organization_id=self.ctx_org_id
            ).exists()
        )()

    async def _handle_acquire_page_edit(self, msg: dict[str, Any]):
        model_id = str(msg.get("model_id") or "")
        page = str(msg.get("page") or "").strip().lower()
        raw_product = msg.get("product_id")
        product_id = str(raw_product) if raw_product else None
        if not model_id or not page:
            return
        if not await self._model_in_org(model_id):
            return

        user = self.scope["user"]
        key = page_edit_lock_key(
            org_id=self.ctx_org_id,
            model_id=model_id,
            page=page,
            product_id=product_id,
        )
        base = {"model_id": model_id, "page": page}
        if product_id:
            base["product_id"] = product_id

        if not _redis_enabled():
            ok = mem_try_page_edit(key=key, user_id=str(user.id), name=self.profile_name)
            if ok:
                await self.send_json(
                    {
                        "type": "page_edit_acquired",
                        "locked_by": user.id,
                        "name": self.profile_name,
                        **base,
                    }
                )
                await self.channel_layer.group_send(
                    self.group,
                    {
                        "type": "broadcast",
                        "payload": {
                            "type": "page_edit_changed",
                            "active": True,
                            "locked_by": user.id,
                            "name": self.profile_name,
                            **base,
                        },
                    },
                )
            else:
                holder = mem_get_page_edit(key)
                await self.send_json(
                    {
                        "type": "page_edit_denied",
                        **base,
                        "locked_by": holder[0] if holder else None,
                        "name": holder[1] if holder else "Another user",
                    }
                )
                if holder:
                    await self.channel_layer.group_send(
                        self.group,
                        {
                            "type": "broadcast",
                            "payload": {
                                "type": "page_edit_changed",
                                "active": True,
                                "locked_by": holder[0],
                                "name": holder[1],
                                **base,
                            },
                        },
                    )
            return

        r = _redis_client()
        try:
            ok = await r.set(key, str(user.id), nx=True, ex=PAGE_EDIT_TTL_SECONDS)
        except Exception:
            ok = True
        if ok:
            try:
                await r.sadd(_user_page_edits_key(org_id=self.ctx_org_id, user_id=user.id), key)
                await r.expire(
                    _user_page_edits_key(org_id=self.ctx_org_id, user_id=user.id),
                    PAGE_EDIT_TTL_SECONDS,
                )
            except Exception:
                pass
            await self.send_json(
                {
                    "type": "page_edit_acquired",
                    "locked_by": user.id,
                    "name": self.profile_name,
                    **base,
                }
            )
            await self.channel_layer.group_send(
                self.group,
                {
                    "type": "broadcast",
                    "payload": {
                        "type": "page_edit_changed",
                        "active": True,
                        "locked_by": user.id,
                        "name": self.profile_name,
                        **base,
                    },
                },
            )
        else:
            try:
                locked_by = await r.get(key)
            except Exception:
                locked_by = None
            holder_name = None
            if locked_by:
                try:
                    holder_name = await r.hget(
                        _presence_key(org_id=self.ctx_org_id), locked_by
                    )
                except Exception:
                    holder_name = None
            holder_name = holder_name or "Another user"
            await self.send_json(
                {
                    "type": "page_edit_denied",
                    **base,
                    "locked_by": locked_by,
                    "name": holder_name,
                }
            )
            if locked_by:
                await self.channel_layer.group_send(
                    self.group,
                    {
                        "type": "broadcast",
                        "payload": {
                            "type": "page_edit_changed",
                            "active": True,
                            "locked_by": locked_by,
                            "name": holder_name,
                            **base,
                        },
                    },
                )

    async def _handle_release_page_edit(self, msg: dict[str, Any]):
        model_id = str(msg.get("model_id") or "")
        page = str(msg.get("page") or "").strip().lower()
        raw_product = msg.get("product_id")
        product_id = str(raw_product) if raw_product else None
        if not model_id or not page:
            return

        user = self.scope["user"]
        key = page_edit_lock_key(
            org_id=self.ctx_org_id,
            model_id=model_id,
            page=page,
            product_id=product_id,
        )
        base = {"model_id": model_id, "page": page}
        if product_id:
            base["product_id"] = product_id

        if not _redis_enabled():
            mem_release_page_edit(key=key, user_id=str(user.id))
        else:
            r = _redis_client()
            try:
                locked_by = await r.get(key)
                if locked_by == str(user.id):
                    await r.delete(key)
                await r.srem(_user_page_edits_key(org_id=self.ctx_org_id, user_id=user.id), key)
            except Exception:
                pass

        await self.channel_layer.group_send(
            self.group,
            {
                "type": "broadcast",
                "payload": {"type": "page_edit_changed", "active": False, **base},
            },
        )


def _page_edit_payload_from_key(key: str, *, active: bool) -> dict[str, Any] | None:
    parts = key.split(":")
    if len(parts) < 5 or parts[0] != "pageedit":
        return None
    model_id = parts[2]
    page = parts[3]
    product_id = parts[4] if len(parts) > 4 else None
    payload: dict[str, Any] = {
        "type": "page_edit_changed",
        "model_id": model_id,
        "page": page,
        "active": active,
    }
    if product_id:
        payload["product_id"] = product_id
    return payload