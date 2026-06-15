"""Check Redis + Channels configuration for realtime collaboration."""

import os

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Verify Redis and Channels channel layer for model collaboration WebSockets."

    def handle(self, *args, **options):
        inmem = bool(getattr(settings, "USE_INMEMORY_CHANNEL_LAYER", False))
        redis_url = getattr(settings, "REDIS_URL", "redis://127.0.0.1:6379/0")
        layer = settings.CHANNEL_LAYERS.get("default", {}).get("BACKEND", "?")

        self.stdout.write(f"DEBUG={settings.DEBUG}")
        self.stdout.write(
            f"env USE_INMEMORY_CHANNEL_LAYER={os.getenv('USE_INMEMORY_CHANNEL_LAYER', '(not set)')!r}"
        )
        self.stdout.write(f"env REDIS_URL={os.getenv('REDIS_URL', '(not set)')!r}")
        self.stdout.write(f"env DEBUG={os.getenv('DEBUG', '(not set)')!r}")
        self.stdout.write(f"USE_INMEMORY_CHANNEL_LAYER={inmem}")
        self.stdout.write(f"CHANNEL_LAYER_BACKEND={layer}")
        self.stdout.write(f"REDIS_URL={redis_url}")

        if inmem:
            self.stdout.write(
                self.style.WARNING(
                    "In-memory channel layer: realtime works on a single Daphne/runserver process only."
                )
            )
            return

        try:
            from redis import Redis

            r = Redis.from_url(redis_url)
            r.ping()
            self.stdout.write(self.style.SUCCESS("Redis PING ok — channel layer + locks can use Redis."))
        except Exception as exc:
            self.stdout.write(
                self.style.ERROR(f"Redis unreachable ({exc}). Set USE_INMEMORY_CHANNEL_LAYER=1 for local dev.")
            )
