"""
One-time / ops backfill for organization membership and model scoping.

Production deploy (run once after release, not on every request):
  python manage.py provision_orphan_users
  python manage.py provision_orphan_users --repair-model-links

Login uses ensure_user_organization_if_needed() with cheap EXISTS checks only.
"""
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from apps.organizations.provisioning import (
    ensure_user_organization,
    user_ids_with_mislinked_models,
    users_needing_organization,
)


class Command(BaseCommand):
    help = (
        "Backfill personal organizations and relink RMCT models. "
        "Run once on deploy; login does not scan all users."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List affected users without writing to the DB.",
        )
        parser.add_argument(
            "--repair-model-links",
            action="store_true",
            help=(
                "Also repair users who have membership but models on Default/null org. "
                "Heavier than the default orphan-only pass."
            ),
        )
        parser.add_argument(
            "--all-users",
            action="store_true",
            help="Deprecated alias for --repair-model-links (runs repair pass only).",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=200,
            help="Iterator chunk size when scanning users (default 200).",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        repair_links = options["repair_model_links"] or options["all_users"]
        batch_size = max(1, options["batch_size"])

        orphan_ids = set()
        for user in users_needing_organization():
            orphan_ids.add(user.id)

        repair_ids = set()
        if repair_links:
            for user in user_ids_with_mislinked_models().iterator(chunk_size=batch_size):
                repair_ids.add(user.id)

        target_ids = sorted(orphan_ids | repair_ids)
        self.stdout.write(
            f"Targets: {len(orphan_ids)} without membership, "
            f"{len(repair_ids)} with model-link repair, "
            f"{len(target_ids)} total."
        )

        if dry_run:
            for uid in target_ids:
                u = User.objects.filter(id=uid).only("id", "email").first()
                if u:
                    self.stdout.write(f"  would provision: {u.id} {u.email}")
            return

        created = 0
        repaired = 0
        models_total = 0
        errors = 0

        for uid in target_ids:
            user = User.objects.filter(id=uid).first()
            if not user:
                continue
            try:
                org, was_created, models_linked = ensure_user_organization(
                    user,
                    force_model_relink=uid in repair_ids,
                )
            except Exception as exc:
                errors += 1
                self.stderr.write(f"ERROR {user.email}: {exc}")
                continue

            models_total += models_linked
            if was_created:
                created += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Created org '{org.name}' for {user.email} "
                        f"(linked {models_linked} model(s))"
                    )
                )
            else:
                repaired += 1
                if models_linked:
                    self.stdout.write(
                        f"Repaired {user.email} -> {org.name} "
                        f"(linked {models_linked} model(s))"
                    )

        summary = (
            f"Done. created={created} repaired={repaired} "
            f"models_linked={models_total} errors={errors}"
        )
        if errors:
            self.stderr.write(self.style.ERROR(summary))
        else:
            self.stdout.write(self.style.SUCCESS(summary))
