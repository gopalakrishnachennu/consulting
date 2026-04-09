"""
Seed 5 built-in ATS-friendly resume templates.

Usage:
    python manage.py seed_resume_templates
    python manage.py seed_resume_templates --force   # overwrite existing
"""
from django.core.management.base import BaseCommand
from resumes.models import ResumeTemplate

TEMPLATES = [
    {
        'name': 'Classic',
        'slug': 'classic',
        'font_family': 'Georgia, serif',
        'name_size': 22, 'header_size': 13, 'body_size': 11, 'contact_size': 10,
        'accent_color': '#111827', 'name_color': '#111827', 'body_color': '#374151',
        'margin_top': 0.75, 'margin_bottom': 0.75, 'margin_left': 0.75, 'margin_right': 0.75,
        'line_height': 1.30, 'para_spacing': 5, 'section_spacing': 10,
        'header_style': 'underline', 'show_dividers': True, 'bullet_char': '•',
    },
    {
        'name': 'Modern',
        'slug': 'modern',
        'font_family': '"Helvetica Neue", Arial, sans-serif',
        'name_size': 24, 'header_size': 12, 'body_size': 11, 'contact_size': 10,
        'accent_color': '#1d4ed8', 'name_color': '#1e3a5f', 'body_color': '#374151',
        'margin_top': 0.70, 'margin_bottom': 0.70, 'margin_left': 0.75, 'margin_right': 0.75,
        'line_height': 1.35, 'para_spacing': 5, 'section_spacing': 10,
        'header_style': 'bar', 'show_dividers': True, 'bullet_char': '›',
    },
    {
        'name': 'Executive',
        'slug': 'executive',
        'font_family': '"Times New Roman", serif',
        'name_size': 26, 'header_size': 13, 'body_size': 11, 'contact_size': 10,
        'accent_color': '#7c2d12', 'name_color': '#111827', 'body_color': '#1f2937',
        'margin_top': 0.80, 'margin_bottom': 0.80, 'margin_left': 0.85, 'margin_right': 0.85,
        'line_height': 1.35, 'para_spacing': 6, 'section_spacing': 11,
        'header_style': 'caps', 'show_dividers': True, 'bullet_char': '▪',
    },
    {
        'name': 'Slate',
        'slug': 'slate',
        'font_family': 'Garamond, Georgia, serif',
        'name_size': 22, 'header_size': 12, 'body_size': 11, 'contact_size': 10,
        'accent_color': '#334155', 'name_color': '#0f172a', 'body_color': '#334155',
        'margin_top': 0.75, 'margin_bottom': 0.75, 'margin_left': 0.75, 'margin_right': 0.75,
        'line_height': 1.30, 'para_spacing': 5, 'section_spacing': 10,
        'header_style': 'underline', 'show_dividers': True, 'bullet_char': '◦',
    },
    {
        'name': 'ATS Pure',
        'slug': 'ats-pure',
        'font_family': 'Arial, sans-serif',
        'name_size': 20, 'header_size': 12, 'body_size': 11, 'contact_size': 10,
        'accent_color': '#000000', 'name_color': '#000000', 'body_color': '#000000',
        'margin_top': 0.75, 'margin_bottom': 0.75, 'margin_left': 0.75, 'margin_right': 0.75,
        'line_height': 1.20, 'para_spacing': 4, 'section_spacing': 8,
        'header_style': 'plain', 'show_dividers': False, 'bullet_char': '-',
    },
]


class Command(BaseCommand):
    help = 'Seed 5 built-in ATS resume templates'

    def add_arguments(self, parser):
        parser.add_argument('--force', action='store_true', help='Overwrite existing templates')

    def handle(self, *args, **options):
        force = options['force']
        created_count = 0
        updated_count = 0

        for cfg in TEMPLATES:
            slug = cfg['slug']
            exists = ResumeTemplate.objects.filter(slug=slug).first()

            if exists and not force:
                self.stdout.write(f'  SKIP  {cfg["name"]} (already exists, use --force to overwrite)')
                continue

            if exists:
                for k, v in cfg.items():
                    setattr(exists, k, v)
                exists.is_builtin = True
                exists.save()
                updated_count += 1
                self.stdout.write(self.style.WARNING(f'  UPDATE {cfg["name"]}'))
            else:
                ResumeTemplate.objects.create(is_builtin=True, **cfg)
                created_count += 1
                self.stdout.write(self.style.SUCCESS(f'  CREATE {cfg["name"]}'))

        self.stdout.write(
            self.style.SUCCESS(
                f'\nDone — {created_count} created, {updated_count} updated.'
            )
        )
