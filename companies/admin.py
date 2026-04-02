from django.contrib import admin, messages

from .models import Company, CompanyDoNotSubmit
from .services import find_potential_duplicate_companies


class CompanyDoNotSubmitInline(admin.TabularInline):
    model = CompanyDoNotSubmit
    extra = 0


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "website",
        "website_is_valid",
        "industry",
        "relationship_status",
        "is_blacklisted",
        "total_submissions",
        "total_placements",
        "last_activity_at",
    )
    search_fields = ("name", "alias", "website", "industry", "primary_contact_name", "primary_contact_email")
    list_filter = ("relationship_status", "industry", "is_blacklisted")
    inlines = [CompanyDoNotSubmitInline]

    def save_model(self, request, obj, form, change):
        """
        On create, warn admins if a similar company already exists (fuzzy name + domain match).
        """
        creating = obj.pk is None
        super().save_model(request, obj, form, change)

        if creating:
            duplicates = find_potential_duplicate_companies(obj.name, obj.website)
            # Exclude self
            duplicates = [(c, score) for (c, score) in duplicates if c.pk != obj.pk]
            if duplicates:
                parts = [
                    f"{c.name} (id={c.pk}, score={score:.2f})"
                    for (c, score) in duplicates
                ]
                msg = (
                    "Potential duplicate companies detected: "
                    + "; ".join(parts)
                    + ". Consider merging or reusing an existing company."
                )
                messages.warning(request, msg)
