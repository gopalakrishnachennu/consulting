from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings


class Migration(migrations.Migration):

    dependencies = [
        ("jobs", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("harvest", "0074_remove_vetgateconfig_allow_possible_filter_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="DeadLinkReviewItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("pending", "Pending review"), ("dismissed", "Dismissed"), ("archived", "Archived"), ("purged", "Purged")], db_index=True, default="pending", max_length=16)),
                ("link_health_reason", models.CharField(blank=True, max_length=120)),
                ("link_health_state", models.CharField(blank=True, max_length=16)),
                ("link_checked_at", models.DateTimeField(blank=True, null=True)),
                ("submission_count", models.PositiveIntegerField(default=0)),
                ("flagged_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("review_note", models.CharField(blank=True, max_length=500)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("linked_job", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="dead_link_reviews", to="jobs.job")),
                ("raw_job", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="dead_link_review", to="harvest.rawjob")),
                ("reviewed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "verbose_name": "Dead link review item",
                "verbose_name_plural": "Dead link review items",
                "ordering": ["-flagged_at"],
            },
        ),
        migrations.AddIndex(
            model_name="deadlinkreviewitem",
            index=models.Index(fields=["status", "-flagged_at"], name="dead_link_status_flagged_idx"),
        ),
    ]
