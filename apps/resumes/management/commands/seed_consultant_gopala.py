"""
Management command: seed_consultant_gopala

Creates a fully populated ConsultantProfile for Gopala Krishna Chennu
with real experience, education, certifications, and skills data
sourced from his actual resumes.

Usage:
    python manage.py seed_consultant_gopala
    python manage.py seed_consultant_gopala --update   # refresh existing
"""
import datetime
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model


BASE_RESUME_TEXT = """Gopala Krishna Chennu
Jersey City, NJ | chennugopalakrishna2@gmail.com | 347-470-9287

PROFESSIONAL SUMMARY
Database Engineer with 5+ years of experience architecting, optimizing, and securing NoSQL database ecosystems in AWS Cloud. Expert in OpenSearch, Elasticsearch, Cassandra, Solr, and Redis, with proven success in ensuring high availability, resilience, and cost efficiency across mission-critical environments. Adept at leveraging Python, CloudFormation, and Linux to automate deployments, enhance monitoring, and enforce robust compliance controls. Recognized for cross-functional collaboration, stakeholder engagement, and Agile teamwork that drive innovation, operational excellence, and measurable business impact.

PROFESSIONAL EXPERIENCE

Database Engineer | ExxonMobil | Jul 2025 – Present
- Architected and managed NoSQL clusters using Elasticsearch, OpenSearch, and Redis in AWS, improving data retrieval performance by 30% and ensuring continuous 24x7 uptime for enterprise workloads.
- Automated maintenance and monitoring with Python and CloudWatch, achieving 40% faster incident response while maintaining HA/DR readiness and ISO-level compliance.
- Implemented CloudFormation templates for infrastructure automation, reducing provisioning times by 35% and standardizing configurations across development and production environments.
- Designed and executed security patching and access control policies with IAM and KMS, mitigating vulnerabilities and enhancing protection for critical customer data.
- Led query optimization and indexing initiatives for Cassandra and Solr, achieving a 25% reduction in query latency and improving application response time.
- Deployed data retention and backup automation strategies, reducing cloud storage costs by 10% while maintaining compliance with regulatory data retention requirements.
- Provided mentorship to junior DBAs through training sessions on NoSQL and AWS best practices, fostering a culture of continuous learning and process improvement.
- Delivered on-call operational support for critical incidents, meeting SLAs and ensuring rapid recovery from system outages with minimal service disruption.
- Spearheaded the implementation of automated backup and disaster recovery workflows using AWS Lambda and CloudFormation, ensuring zero data loss and improving recovery time objectives by 45%.

Cloud Database Engineer | Thomson Reuters | Oct 2024 – May 2025
- Supported administration of Cassandra, Solr, and OpenSearch clusters in AWS, enhancing scalability and achieving a 20% increase in data throughput and reliability.
- Led migration of legacy on-prem databases to AWS DynamoDB and DocumentDB, achieving 30% reduction in infrastructure cost and strengthening data resiliency.
- Developed and maintained CloudFormation IaC templates, improving environment setup efficiency and ensuring consistent configurations across global development teams.
- Collaborated with developers to fine-tune queries and schema design, resulting in a 15% improvement in response times and enhanced customer experience.
- Implemented automated monitoring using CloudWatch and OpenSearch Dashboards, enabling early detection of issues and preventing performance degradation.
- Authored detailed operational runbooks and compliance documentation, supporting internal audits and knowledge transfer across engineering and operations teams.

Database Administrator | Tiger Analytics | Sep 2019 – Jul 2023
- Administered Cassandra, Redis, and Solr databases in hybrid environments, maintaining 99.9% uptime and 20% faster data delivery for analytics workloads.
- Designed and implemented security patching and encryption strategies, aligning with enterprise compliance frameworks and minimizing exposure risks.
- Automated performance tuning and backup processes using Python and shell scripts, reducing manual workload by 50% and improving data recovery success rate to 99.9%.
- Partnered with data engineers to optimize ETL processes and indexing workflows, reducing pipeline execution times by 30% and improving data accuracy.
- Introduced proactive monitoring dashboards via OpenSearch and CloudWatch, leading to a 20% reduction in system downtime and improved operational visibility.
- Conducted post-incident reviews and root cause analysis to enhance HA/DR architecture, ensuring resilience and sustained service delivery for business-critical applications.
- Installed and configured SQL Server databases across cloud and on-premises environments, performing root cause analysis and HA/DR measures ensuring compliance.
- Developed and maintained complex SSIS packages and ETL workflows, streamlining cloud migration processes and data integration efficiency.
- Monitored database performance using Redgate, SolarWinds, CloudWatch, and Log Analytics, tuning queries and indexes to enhance response times by 20%.
- Collaborated with cross-functional teams to implement CI/CD pipelines using Azure DevOps and Jenkins, supporting event-driven database deployments.
- Supported high availability and failover configurations using AlwaysOn Availability Groups, maintaining near-zero downtime for mission-critical data systems.
- Automated database monitoring, performance analysis, and root cause debugging using Python scripts and SolarWinds tools, reducing manual effort by 40%.
- Designed and enforced security controls and access policies, achieving compliance with HIPAA and organizational data governance standards.

EDUCATION
Master of Science in Information Technology Management — Wilmington University, USA
Bachelor of Technology in Computer Science — SRM University, India

DOMAIN EXPERIENCE
Energy/Oil & Gas (ExxonMobil), Media/Legal Tech (Thomson Reuters), Analytics/SaaS (Tiger Analytics)
Healthcare IT systems — HIPAA compliance experience
Financial services data infrastructure

NOTES
- Available for hybrid or remote roles in NJ/TX metro
- Strong NoSQL + cloud stack; also has SQL Server / SSIS experience for DBA-type roles
- Comfortable as Database Engineer, Cloud DBA, DBRE, or Platform Engineer
- Proficient across AWS; familiar with Azure DevOps for CI/CD
"""

SKILLS = [
    # Cloud
    "AWS", "EC2", "S3", "Lambda", "DynamoDB", "DocumentDB", "AWS CDK", "ECS", "EKS",
    # Databases – NoSQL
    "OpenSearch", "Elasticsearch", "Cassandra", "Apache Solr", "Solr", "Redis",
    # Databases – Relational
    "SQL Server", "Aurora", "PostgreSQL", "MySQL",
    # Data / ETL
    "SSIS", "ETL", "Data Warehousing",
    # IaC
    "CloudFormation", "Terraform", "ARM Templates",
    # CI/CD
    "Jenkins", "GitHub Actions", "AWS CodePipeline", "Azure DevOps", "CI/CD Pipelines",
    # Containers
    "Docker", "Kubernetes", "EKS",
    # Scripting
    "Python", "Bash", "Shell scripting", "T-SQL", "PL/SQL", "PowerShell",
    # Monitoring
    "CloudWatch", "OpenSearch Dashboards", "Prometheus", "Grafana", "SolarWinds", "Redgate", "Log Analytics",
    # Security
    "IAM", "KMS", "RBAC", "Encryption", "Security Patching", "Data Masking", "HIPAA", "SOC2",
    # Ops
    "High Availability", "Disaster Recovery", "HA/DR", "AlwaysOn Availability Groups", "Backup & Recovery",
    "Query Optimization", "Index Management", "Performance Tuning",
    # Collab
    "JIRA", "Confluence", "Git", "Agile", "SharePoint",
]

EXPERIENCE_DATA = [
    {
        "title": "Database Engineer",
        "company": "ExxonMobil",
        "start_date": datetime.date(2025, 7, 1),
        "end_date": None,
        "is_current": True,
        "description": (
            "Architect and manage NoSQL clusters using Elasticsearch, OpenSearch, and Redis in AWS, "
            "improving data retrieval performance by 30% and ensuring 24x7 uptime for enterprise workloads. "
            "Automate maintenance and monitoring with Python and CloudWatch, achieving 40% faster incident "
            "response while maintaining HA/DR readiness and ISO-level compliance. "
            "Implement CloudFormation templates for infrastructure automation, reducing provisioning times by 35% "
            "and standardizing configurations across development and production environments. "
            "Design and execute security patching and access control policies with IAM and KMS, mitigating "
            "vulnerabilities and enhancing protection for critical customer data. "
            "Lead query optimization and indexing initiatives for Cassandra and Solr, achieving 25% reduction "
            "in query latency and improving application response time. "
            "Deploy data retention and backup automation strategies, reducing cloud storage costs by 10% "
            "while maintaining compliance with regulatory data retention requirements. "
            "Provide mentorship to junior DBAs on NoSQL and AWS best practices. "
            "Deliver on-call operational support for critical incidents, meeting SLAs and ensuring rapid recovery. "
            "Spearhead automated backup and disaster recovery workflows using AWS Lambda and CloudFormation, "
            "ensuring zero data loss and improving recovery time objectives by 45%."
        ),
    },
    {
        "title": "Cloud Database Engineer",
        "company": "Thomson Reuters",
        "start_date": datetime.date(2024, 10, 1),
        "end_date": datetime.date(2025, 5, 1),
        "is_current": False,
        "description": (
            "Supported administration of Cassandra, Solr, and OpenSearch clusters in AWS, enhancing scalability "
            "and achieving 20% increase in data throughput and reliability. "
            "Led migration of legacy on-prem databases to AWS DynamoDB and DocumentDB, achieving 30% reduction "
            "in infrastructure cost and strengthening data resiliency. "
            "Developed and maintained CloudFormation IaC templates, improving environment setup efficiency "
            "and ensuring consistent configurations across global development teams. "
            "Collaborated with developers to fine-tune queries and schema design, resulting in 15% improvement "
            "in response times and enhanced customer experience. "
            "Implemented automated monitoring using CloudWatch and OpenSearch Dashboards, enabling early detection "
            "of issues and preventing performance degradation. "
            "Authored detailed operational runbooks and compliance documentation, supporting internal audits "
            "and knowledge transfer across engineering and operations teams. "
            "Configured and secured cloud-based SQL Server environments, implementing best practices for Aurora "
            "and network segmentation. "
            "Designed and enforced security controls and access policies achieving compliance with HIPAA and "
            "organizational data governance standards."
        ),
    },
    {
        "title": "Database Administrator",
        "company": "Tiger Analytics",
        "start_date": datetime.date(2019, 9, 1),
        "end_date": datetime.date(2023, 7, 1),
        "is_current": False,
        "description": (
            "Administered Cassandra, Redis, and Solr databases in hybrid environments, maintaining 99.9% uptime "
            "and 20% faster data delivery for analytics workloads. "
            "Designed and implemented security patching and encryption strategies, aligning with enterprise "
            "compliance frameworks and minimizing exposure risks. "
            "Automated performance tuning and backup processes using Python and shell scripts, reducing manual "
            "workload by 50% and improving data recovery success rate to 99.9%. "
            "Partnered with data engineers to optimize ETL processes and indexing workflows, reducing pipeline "
            "execution times by 30% and improving data accuracy. "
            "Introduced proactive monitoring dashboards via OpenSearch and CloudWatch, leading to 20% reduction "
            "in system downtime and improved operational visibility. "
            "Conducted post-incident reviews and root cause analysis to enhance HA/DR architecture. "
            "Managed SQL Server databases with SSIS packages and ETL workflows. "
            "Implemented CI/CD pipelines using Azure DevOps and Jenkins for database deployments. "
            "Supported AlwaysOn Availability Groups for high availability of mission-critical systems. "
            "Performed root cause analysis and debugging to resolve database performance issues, "
            "improving user satisfaction by 15%. "
            "Provided mentoring and training to junior database administrators on monitoring and cloud migration."
        ),
    },
]

EDUCATION_DATA = [
    {
        "degree": "Master of Science",
        "field_of_study": "Information Technology Management",
        "institution": "Wilmington University",
        "start_date": datetime.date(2023, 9, 1),
        "end_date": datetime.date(2025, 5, 1),
        "is_current": False,
    },
    {
        "degree": "Bachelor of Technology",
        "field_of_study": "Computer Science",
        "institution": "SRM University",
        "start_date": datetime.date(2015, 7, 1),
        "end_date": datetime.date(2019, 5, 1),
        "is_current": False,
    },
]


class Command(BaseCommand):
    help = "Seed a real ConsultantProfile for Gopala Krishna Chennu with actual resume data"

    def add_arguments(self, parser):
        parser.add_argument(
            '--update',
            action='store_true',
            help='Update existing profile if it already exists',
        )

    def handle(self, *args, **options):
        from users.models import User, ConsultantProfile
        from users.models import Experience, Education

        User = get_user_model()

        # Find or create user
        user, user_created = User.objects.get_or_create(
            username='gopala.krishna',
            defaults={
                'email': 'chennugopalakrishna2@gmail.com',
                'first_name': 'Gopala Krishna',
                'last_name': 'Chennu',
                'role': 'CONSULTANT',
                'is_active': True,
            }
        )
        if user_created:
            user.set_password('GoCareers@2025!')
            user.save()
            self.stdout.write(f'Created user: {user.username} (password: GoCareers@2025!)')
        else:
            # Update name/email if user already exists
            user.first_name = 'Gopala Krishna'
            user.last_name = 'Chennu'
            user.email = 'chennugopalakrishna2@gmail.com'
            if user.role not in ('CONSULTANT',):
                user.role = 'CONSULTANT'
            user.save()
            self.stdout.write(f'Using existing user: {user.username}')

        # Get or create consultant profile
        profile, created = ConsultantProfile.objects.get_or_create(
            user=user,
            defaults={
                'phone': '347-470-9287',
                'preferred_location': 'Jersey City, NJ',
                'bio': (
                    'Database Engineer with 5+ years of experience in AWS cloud, NoSQL databases '
                    '(OpenSearch, Elasticsearch, Cassandra, Solr, Redis), infrastructure automation, '
                    'and compliance-driven environments. Strong background in performance optimization, '
                    'HA/DR, and cross-functional Agile delivery.'
                ),
                'skills': SKILLS,
                'base_resume_text': BASE_RESUME_TEXT.strip(),
                'status': 'ACTIVE',
                'notice_period': '2 weeks',
            }
        )

        if not created and options['update']:
            profile.phone = '347-470-9287'
            profile.preferred_location = 'Jersey City, NJ'
            profile.bio = (
                'Database Engineer with 5+ years of experience in AWS cloud, NoSQL databases '
                '(OpenSearch, Elasticsearch, Cassandra, Solr, Redis), infrastructure automation, '
                'and compliance-driven environments. Strong background in performance optimization, '
                'HA/DR, and cross-functional Agile delivery.'
            )
            profile.skills = SKILLS
            profile.base_resume_text = BASE_RESUME_TEXT.strip()
            profile.status = 'ACTIVE'
            profile.save(update_fields=['phone', 'preferred_location', 'bio', 'skills', 'base_resume_text', 'status'])
            self.stdout.write('Updated existing profile.')
        elif not created:
            self.stdout.write(
                self.style.WARNING(
                    f'Profile already exists (pk={profile.pk}). Use --update to refresh data.'
                )
            )
            return

        # ── Experience ────────────────────────────────────────────────────────
        if created or options.get('update'):
            Experience.objects.filter(consultant_profile=profile).delete()
            for exp in EXPERIENCE_DATA:
                Experience.objects.create(
                    consultant_profile=profile,
                    title=exp['title'],
                    company=exp['company'],
                    start_date=exp['start_date'],
                    end_date=exp['end_date'],
                    is_current=exp['is_current'],
                    description=exp['description'],
                )
            self.stdout.write(f'  Created {len(EXPERIENCE_DATA)} experience records.')

        # ── Education ─────────────────────────────────────────────────────────
        if created or options.get('update'):
            Education.objects.filter(consultant_profile=profile).delete()
            for edu in EDUCATION_DATA:
                Education.objects.create(
                    consultant_profile=profile,
                    degree=edu['degree'],
                    field_of_study=edu['field_of_study'],
                    institution=edu['institution'],
                    start_date=edu['start_date'],
                    end_date=edu['end_date'],
                )
            self.stdout.write(f'  Created {len(EDUCATION_DATA)} education records.')

        self.stdout.write(
            self.style.SUCCESS(
                f'\nConsultant profile ready:\n'
                f'  Name: {user.get_full_name()}\n'
                f'  PK:   {profile.pk}\n'
                f'  Email: {user.email}\n'
                f'  Phone: {profile.phone}\n'
                f'  Skills: {len(profile.skills)} items\n'
                f'  Experience: {profile.experience.count()} roles\n'
                f'  Education: {profile.education.count()} records\n'
                f'  Base resume: {len(profile.base_resume_text)} chars\n'
                f'\nGo to /resumes/generate/ and select "Gopala Krishna Chennu" to generate.'
            )
        )
