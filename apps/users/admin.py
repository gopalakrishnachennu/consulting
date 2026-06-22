from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import (
    User,
    ConsultantProfile,
    EmployeeProfile,
    ConsultantLead,
    EmployerAccessRequest,
    Experience,
    Education,
    Certification,
    Department,
    MarketingRole,
)

@admin.register(User)
class CustomUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (
        (None, {'fields': ('role', 'avatar')}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        (None, {'fields': ('role', 'avatar')}),
    )
    list_display = ('username', 'email', 'role', 'is_staff')
    list_filter = ('role', 'is_staff', 'is_active')

class ExperienceInline(admin.StackedInline):
    model = Experience
    extra = 0

class EducationInline(admin.StackedInline):
    model = Education
    extra = 0
    
class CertificationInline(admin.StackedInline):
    model = Certification
    extra = 0

@admin.register(ConsultantProfile)
class ConsultantProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'hourly_rate')
    inlines = [ExperienceInline, EducationInline, CertificationInline]

@admin.register(EmployeeProfile)
class EmployeeProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'department', 'company_name', 'phone', 'work_location')


@admin.register(ConsultantLead)
class ConsultantLeadAdmin(admin.ModelAdmin):
    list_display = ("full_name", "email", "current_title", "status", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("full_name", "email", "current_title", "preferred_markets")


@admin.register(EmployerAccessRequest)
class EmployerAccessRequestAdmin(admin.ModelAdmin):
    list_display = ("company_name", "contact_name", "work_email", "status", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("company_name", "contact_name", "work_email")

@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ('name', 'created_at')
    search_fields = ('name',)


@admin.register(MarketingRole)
class MarketingRoleAdmin(admin.ModelAdmin):
    list_display  = (
        'name', 'slug', 'top_category', 'is_active',
        'display_order', 'keyword_count',
    )
    list_filter   = ('top_category', 'is_active')
    search_fields = ('name', 'slug', 'description')
    prepopulated_fields = {'slug': ('name',)}
    ordering      = ('display_order', 'name')
    list_editable = ('is_active', 'display_order')

    fieldsets = (
        (None, {
            'fields': ('name', 'slug', 'top_category', 'is_active', 'display_order'),
        }),
        ('Description', {
            'fields': ('description',),
        }),
        ('Auto-classification Keywords', {
            'description': (
                'Lowercase keyword phrases matched against job title and description. '
                'One phrase per line. The engine checks title first, then description. '
                'More specific phrases should come before generic ones.'
            ),
            'fields': ('match_keywords',),
        }),
    )

    @admin.display(description='# Keywords')
    def keyword_count(self, obj):
        return len(obj.match_keywords or [])
