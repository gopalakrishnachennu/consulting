"""
Comprehensive seed data command for GoCareers platform.
Creates realistic test data across all models to verify end-to-end functionality.
"""
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.utils import timezone

User = get_user_model()


class Command(BaseCommand):
    help = "Seeds database with comprehensive test data for all apps"

    def add_arguments(self, parser):
        parser.add_argument(
            "--flush",
            action="store_true",
            help="Delete all existing data before seeding",
        )

    def handle(self, *args, **options):
        if options["flush"]:
            self.stdout.write(self.style.WARNING("Flushing existing data..."))
            self._flush()

        self.stdout.write("Seeding data...")
        self._seed_users()
        self._seed_platform_config()
        self._seed_llm_config()
        self._seed_marketing_roles()
        self._seed_companies()
        self._seed_jobs()
        self._seed_experience_education()
        self._seed_submissions()
        self._seed_interviews()
        self._seed_prompts()
        self._seed_messages()
        self._seed_placements()
        self.stdout.write(self.style.SUCCESS("\nAll seed data created successfully!"))

    def _flush(self):
        from submissions.models import ApplicationSubmission, Placement, Timesheet, Commission
        from jobs.models import Job
        from companies.models import Company
        from users.models import MarketingRole, ConsultantProfile, EmployeeProfile
        from interviews_app.models import Interview
        from messaging.models import Thread
        from prompts_app.models import Prompt

        Commission.objects.all().delete()
        Timesheet.objects.all().delete()
        Placement.objects.all().delete()
        Interview.objects.all().delete()
        ApplicationSubmission.objects.all().delete()
        Job.objects.all().delete()
        Company.objects.all().delete()
        Prompt.objects.all().delete()
        Thread.objects.all().delete()
        MarketingRole.objects.all().delete()
        ConsultantProfile.objects.all().delete()
        EmployeeProfile.objects.all().delete()
        User.objects.filter(is_superuser=False).delete()
        self.stdout.write(self.style.SUCCESS("  Flushed."))

    # ── Users ──────────────────────────────────────────────────────────
    def _seed_users(self):
        from users.models import ConsultantProfile, EmployeeProfile, Department

        # Admin
        if not User.objects.filter(username="admin").exists():
            User.objects.create_superuser("admin", "admin@gocareers.io", "admin123")
            self.stdout.write(self.style.SUCCESS("  Created superuser: admin / admin123"))

        # Departments
        hr_dept, _ = Department.objects.get_or_create(name="Human Resources")
        eng_dept, _ = Department.objects.get_or_create(name="Engineering")
        sales_dept, _ = Department.objects.get_or_create(name="Sales")

        # Employees
        employees_data = [
            ("sarah_hr", "Sarah", "Johnson", "sarah@gocareers.io", hr_dept, True),
            ("mike_eng", "Mike", "Chen", "mike@gocareers.io", eng_dept, False),
            ("lisa_sales", "Lisa", "Park", "lisa@gocareers.io", sales_dept, True),
        ]
        for uname, first, last, email, dept, can_manage in employees_data:
            if not User.objects.filter(username=uname).exists():
                u = User.objects.create_user(uname, email, "password123", first_name=first, last_name=last)
                u.role = User.Role.EMPLOYEE
                u.save()
                EmployeeProfile.objects.create(
                    user=u, department=dept, company_name="GoCareers Inc.", can_manage_consultants=can_manage,
                )
                self.stdout.write(self.style.SUCCESS(f"  Created employee: {uname}"))

        # Consultants
        consultants_data = [
            {
                "username": "john_dev",
                "first_name": "John",
                "last_name": "Smith",
                "email": "john@example.com",
                "bio": "Senior Full Stack Developer with 10+ years in Python, Django, React, and AWS. "
                       "Led teams of 5-8 engineers building SaaS platforms.",
                "skills": ["Python", "Django", "React", "TypeScript", "AWS", "Docker", "PostgreSQL", "Redis"],
                "hourly_rate": Decimal("150.00"),
                "phone": "+1-555-0101",
                "status": "ACTIVE",
            },
            {
                "username": "alice_devops",
                "first_name": "Alice",
                "last_name": "Wang",
                "email": "alice@example.com",
                "bio": "DevOps & Cloud Architect specializing in Kubernetes, Terraform, and CI/CD. "
                       "AWS Solutions Architect Professional certified.",
                "skills": ["Kubernetes", "Terraform", "AWS", "GCP", "Docker", "Jenkins", "Ansible", "Linux"],
                "hourly_rate": Decimal("175.00"),
                "phone": "+1-555-0102",
                "status": "ACTIVE",
            },
            {
                "username": "bob_data",
                "first_name": "Bob",
                "last_name": "Martinez",
                "email": "bob@example.com",
                "bio": "Data Engineer & ML specialist. Built real-time data pipelines processing 10M+ events/day. "
                       "Strong Python and Spark background.",
                "skills": ["Python", "Spark", "Airflow", "SQL", "Snowflake", "dbt", "Kafka", "ML/AI"],
                "hourly_rate": Decimal("160.00"),
                "phone": "+1-555-0103",
                "status": "BENCH",
            },
            {
                "username": "carol_mobile",
                "first_name": "Carol",
                "last_name": "Davis",
                "email": "carol@example.com",
                "bio": "Mobile developer with 7 years of experience in React Native and Swift. "
                       "Published 15+ apps on App Store and Google Play.",
                "skills": ["React Native", "Swift", "Kotlin", "TypeScript", "Firebase", "GraphQL"],
                "hourly_rate": Decimal("140.00"),
                "phone": "+1-555-0104",
                "status": "PLACED",
            },
            {
                "username": "dave_security",
                "first_name": "Dave",
                "last_name": "Thompson",
                "email": "dave@example.com",
                "bio": "Security Engineer with CISSP and CEH. Specializes in AppSec, penetration testing, "
                       "and cloud security posture management.",
                "skills": ["AppSec", "Penetration Testing", "AWS Security", "SIEM", "Python", "Go"],
                "hourly_rate": Decimal("185.00"),
                "phone": "+1-555-0105",
                "status": "ACTIVE",
            },
        ]
        for c in consultants_data:
            if not User.objects.filter(username=c["username"]).exists():
                u = User.objects.create_user(
                    c["username"], c["email"], "password123",
                    first_name=c["first_name"], last_name=c["last_name"],
                )
                u.role = User.Role.CONSULTANT
                u.save()
                ConsultantProfile.objects.create(
                    user=u, bio=c["bio"], skills=c["skills"],
                    hourly_rate=c["hourly_rate"], phone=c["phone"],
                    status=c["status"],
                )
                self.stdout.write(self.style.SUCCESS(f"  Created consultant: {c['username']}"))

    # ── Platform Config ───────────────────────────────────────────────
    def _seed_platform_config(self):
        from core.models import PlatformConfig

        config = PlatformConfig.load()
        if config.site_name == "EduConsult":
            config.site_name = "GoCareers"
            config.site_tagline = "Connecting Top Tech Talent with Opportunities"
            config.contact_email = "hello@gocareers.io"
            config.support_phone = "+1-800-GO-CAREER"
            config.save()
            self.stdout.write(self.style.SUCCESS("  Updated PlatformConfig"))

    # ── LLM Config ────────────────────────────────────────────────────
    def _seed_llm_config(self):
        from core.models import LLMConfig

        config = LLMConfig.load()
        config.active_model = "gpt-4o-mini"
        config.temperature = Decimal("0.70")
        config.max_output_tokens = 2000
        config.generation_enabled = True
        config.save()
        self.stdout.write(self.style.SUCCESS("  Updated LLMConfig"))

    # ── Marketing Roles ───────────────────────────────────────────────
    def _seed_marketing_roles(self):
        from users.models import MarketingRole, ConsultantProfile

        roles_data = [
            ("DevOps Engineer", "CI/CD pipelines, infrastructure automation, cloud deployments."),
            ("Cloud Architect", "Scalable cloud infrastructure solutions."),
            ("Full Stack Developer", "Frontend and backend web application development."),
            ("Backend Developer", "Server-side logic, APIs, and database integrations."),
            ("Data Engineer", "Data pipelines, ETL, and warehousing."),
            ("ML/AI Engineer", "Machine learning models and AI applications."),
            ("Security Engineer", "Security audits, vulnerability management."),
            ("Mobile Developer", "Native and cross-platform mobile apps."),
            ("SRE / Reliability Engineer", "System reliability, monitoring, incident response."),
            ("QA / Test Engineer", "Testing strategies and software quality."),
        ]
        roles = {}
        for name, desc in roles_data:
            role, _ = MarketingRole.objects.get_or_create(name=name, defaults={"description": desc})
            roles[name] = role

        # Assign roles to consultants
        role_map = {
            "john_dev": ["Full Stack Developer", "Backend Developer"],
            "alice_devops": ["DevOps Engineer", "Cloud Architect", "SRE / Reliability Engineer"],
            "bob_data": ["Data Engineer", "ML/AI Engineer", "Backend Developer"],
            "carol_mobile": ["Mobile Developer", "Full Stack Developer"],
            "dave_security": ["Security Engineer", "DevOps Engineer"],
        }
        for username, role_names in role_map.items():
            try:
                profile = ConsultantProfile.objects.get(user__username=username)
                for rn in role_names:
                    if rn in roles:
                        profile.marketing_roles.add(roles[rn])
            except ConsultantProfile.DoesNotExist:
                pass
        self.stdout.write(self.style.SUCCESS("  Created marketing roles and assigned to consultants"))

    # ── Companies ─────────────────────────────────────────────────────
    def _seed_companies(self):
        from companies.models import Company

        companies_data = [
            {
                "name": "TechCorp Solutions",
                "domain": "techcorp.com",
                "website": "https://techcorp.com",
                "industry": "Technology",
                "size_band": "enterprise",
                "headcount_range": "1000+",
                "hq_location": "San Francisco, CA",
                "relationship_status": "Hot",
                "primary_contact_name": "Rachel Green",
                "primary_contact_email": "rachel@techcorp.com",
            },
            {
                "name": "DataFlow Inc",
                "domain": "dataflow.io",
                "website": "https://dataflow.io",
                "industry": "Data Analytics",
                "size_band": "mid-market",
                "headcount_range": "201-1000",
                "hq_location": "Austin, TX",
                "relationship_status": "Warm",
                "primary_contact_name": "James Wilson",
                "primary_contact_email": "james@dataflow.io",
            },
            {
                "name": "CloudNine Systems",
                "domain": "cloudnine.dev",
                "website": "https://cloudnine.dev",
                "industry": "Cloud Infrastructure",
                "size_band": "startup",
                "headcount_range": "51-200",
                "hq_location": "Seattle, WA",
                "relationship_status": "Hot",
            },
            {
                "name": "SecureVault Corp",
                "domain": "securevault.com",
                "website": "https://securevault.com",
                "industry": "Cybersecurity",
                "size_band": "mid-market",
                "headcount_range": "201-1000",
                "hq_location": "Washington, DC",
                "relationship_status": "Warm",
            },
            {
                "name": "MobileFirst Labs",
                "domain": "mobilefirst.io",
                "website": "https://mobilefirst.io",
                "industry": "Mobile Technology",
                "size_band": "startup",
                "headcount_range": "11-50",
                "hq_location": "New York, NY",
                "relationship_status": "Cold",
            },
            {
                "name": "BadActor LLC",
                "domain": "badactor.biz",
                "website": "https://badactor.biz",
                "industry": "Unknown",
                "size_band": "startup",
                "hq_location": "Unknown",
                "is_blacklisted": True,
                "blacklist_reason": "Multiple NDA violations reported by consultants.",
            },
        ]
        for cd in companies_data:
            Company.objects.get_or_create(name=cd.pop("name"), defaults=cd)
        self.stdout.write(self.style.SUCCESS("  Created 6 companies (1 blacklisted)"))

    # ── Jobs ──────────────────────────────────────────────────────────
    def _seed_jobs(self):
        from jobs.models import Job
        from companies.models import Company
        from users.models import MarketingRole

        employee = User.objects.filter(role=User.Role.EMPLOYEE).first()
        if not employee:
            return

        jobs_data = [
            {
                "title": "Senior Python Developer",
                "company": "TechCorp Solutions",
                "location": "San Francisco, CA (Hybrid)",
                "description": (
                    "We are looking for a Senior Python Developer to join our platform team.\n\n"
                    "Requirements:\n"
                    "- 5+ years of Python experience\n"
                    "- Strong Django/DRF skills\n"
                    "- Experience with PostgreSQL and Redis\n"
                    "- Familiarity with Docker and Kubernetes\n"
                    "- AWS experience preferred\n\n"
                    "Benefits: Competitive salary, equity, health insurance, 401k match, unlimited PTO."
                ),
                "original_link": "https://techcorp.com/careers/senior-python-dev",
                "salary_range": "$140,000 - $180,000",
                "job_type": Job.JobType.FULL_TIME,
                "status": Job.Status.OPEN,
                "roles": ["Backend Developer", "Full Stack Developer"],
            },
            {
                "title": "DevOps Engineer - Kubernetes",
                "company": "CloudNine Systems",
                "location": "Seattle, WA (Remote OK)",
                "description": (
                    "CloudNine is hiring a DevOps Engineer to scale our infrastructure.\n\n"
                    "Responsibilities:\n"
                    "- Manage Kubernetes clusters across multi-cloud\n"
                    "- Build CI/CD pipelines with GitHub Actions\n"
                    "- Implement Infrastructure as Code with Terraform\n"
                    "- On-call rotation (1 week per month)\n\n"
                    "Stack: K8s, Terraform, AWS/GCP, Prometheus, Grafana"
                ),
                "original_link": "https://cloudnine.dev/jobs/devops-k8s",
                "salary_range": "$150,000 - $190,000",
                "job_type": Job.JobType.CONTRACT,
                "status": Job.Status.OPEN,
                "roles": ["DevOps Engineer", "Cloud Architect", "SRE / Reliability Engineer"],
            },
            {
                "title": "Data Engineer - Real-time Pipelines",
                "company": "DataFlow Inc",
                "location": "Austin, TX (On-site)",
                "description": (
                    "Join DataFlow to build next-gen real-time data pipelines.\n\n"
                    "Requirements:\n"
                    "- 3+ years building data pipelines (Spark, Airflow, Kafka)\n"
                    "- Strong SQL and Python\n"
                    "- Experience with Snowflake or BigQuery\n"
                    "- dbt experience is a plus\n\n"
                    "We process 50M+ events per day and growing."
                ),
                "original_link": "https://dataflow.io/careers/data-engineer",
                "salary_range": "$130,000 - $165,000",
                "job_type": Job.JobType.FULL_TIME,
                "status": Job.Status.OPEN,
                "roles": ["Data Engineer", "ML/AI Engineer"],
            },
            {
                "title": "Security Engineer",
                "company": "SecureVault Corp",
                "location": "Washington, DC (On-site)",
                "description": (
                    "SecureVault needs a Security Engineer for our AppSec team.\n\n"
                    "What you'll do:\n"
                    "- Conduct penetration tests and vulnerability assessments\n"
                    "- Review code for security flaws\n"
                    "- Implement SAST/DAST in CI pipelines\n"
                    "- Manage cloud security posture (AWS)\n\n"
                    "Certifications like CISSP, CEH, or OSCP preferred."
                ),
                "original_link": "https://securevault.com/careers/security-engineer",
                "salary_range": "$155,000 - $195,000",
                "job_type": Job.JobType.FULL_TIME,
                "status": Job.Status.OPEN,
                "roles": ["Security Engineer"],
            },
            {
                "title": "React Native Developer",
                "company": "MobileFirst Labs",
                "location": "New York, NY (Hybrid)",
                "description": (
                    "Build cross-platform mobile experiences at MobileFirst.\n\n"
                    "Requirements:\n"
                    "- 3+ years React Native\n"
                    "- iOS and Android deployment experience\n"
                    "- TypeScript proficiency\n"
                    "- Firebase/Supabase experience\n\n"
                    "Small team, big impact. Ship features weekly."
                ),
                "original_link": "https://mobilefirst.io/jobs/react-native-dev",
                "salary_range": "$120,000 - $150,000",
                "job_type": Job.JobType.FULL_TIME,
                "status": Job.Status.OPEN,
                "roles": ["Mobile Developer", "Full Stack Developer"],
            },
            {
                "title": "Junior Backend Developer (Closed)",
                "company": "TechCorp Solutions",
                "location": "Remote",
                "description": "Entry-level backend role. This position has been filled.",
                "original_link": "https://techcorp.com/careers/junior-backend",
                "salary_range": "$80,000 - $100,000",
                "job_type": Job.JobType.FULL_TIME,
                "status": Job.Status.CLOSED,
                "roles": ["Backend Developer"],
            },
        ]

        companies = {c.name: c for c in Company.objects.all()}

        for jd in jobs_data:
            role_names = jd.pop("roles", [])
            company_name = jd["company"]
            jd["company_obj"] = companies.get(company_name)

            job, created = Job.objects.get_or_create(
                title=jd["title"],
                defaults={**jd, "posted_by": employee},
            )
            if created:
                for rn in role_names:
                    try:
                        role = MarketingRole.objects.get(name=rn)
                        job.marketing_roles.add(role)
                    except MarketingRole.DoesNotExist:
                        pass
        self.stdout.write(self.style.SUCCESS("  Created 6 jobs (1 closed)"))

    # ── Experience & Education ────────────────────────────────────────
    def _seed_experience_education(self):
        from users.models import ConsultantProfile, Experience, Education, Certification

        now = timezone.now().date()

        profiles = {
            cp.user.username: cp
            for cp in ConsultantProfile.objects.select_related("user").all()
        }

        # John Smith - Full Stack
        p = profiles.get("john_dev")
        if p and not p.experience.exists():
            Experience.objects.create(
                consultant_profile=p, title="Senior Software Engineer",
                company="Stripe", start_date=now - timedelta(days=1095),
                is_current=True, description="Led a team of 6 building payment APIs."
            )
            Experience.objects.create(
                consultant_profile=p, title="Software Engineer",
                company="Shopify", start_date=now - timedelta(days=2555),
                end_date=now - timedelta(days=1095),
                description="Built e-commerce backend services."
            )
            Education.objects.create(
                consultant_profile=p, institution="MIT",
                degree="B.S.", field_of_study="Computer Science",
                start_date=now - timedelta(days=4380),
                end_date=now - timedelta(days=2920),
            )

        # Alice Wang - DevOps
        p = profiles.get("alice_devops")
        if p and not p.experience.exists():
            Experience.objects.create(
                consultant_profile=p, title="Cloud Architect",
                company="Netflix", start_date=now - timedelta(days=730),
                is_current=True, description="Designed multi-region K8s deployments."
            )
            Experience.objects.create(
                consultant_profile=p, title="DevOps Engineer",
                company="HashiCorp", start_date=now - timedelta(days=1825),
                end_date=now - timedelta(days=730),
                description="Core contributor to internal Terraform modules."
            )
            Certification.objects.create(
                consultant_profile=p,
                name="AWS Solutions Architect Professional",
                issuing_organization="Amazon Web Services",
                issue_date=now - timedelta(days=365),
            )

        # Bob Martinez - Data
        p = profiles.get("bob_data")
        if p and not p.experience.exists():
            Experience.objects.create(
                consultant_profile=p, title="Senior Data Engineer",
                company="Snowflake", start_date=now - timedelta(days=1460),
                is_current=True, description="Built real-time ingestion processing 10M events/day."
            )
            Education.objects.create(
                consultant_profile=p, institution="Stanford University",
                degree="M.S.", field_of_study="Data Science",
                start_date=now - timedelta(days=3650),
                end_date=now - timedelta(days=2920),
            )

        self.stdout.write(self.style.SUCCESS("  Created experience, education, and certifications"))

    # ── Submissions ───────────────────────────────────────────────────
    def _seed_submissions(self):
        from submissions.models import (
            ApplicationSubmission, SubmissionStatusHistory,
            Offer, OfferRound,
        )
        from jobs.models import Job
        from users.models import ConsultantProfile

        employee = User.objects.filter(role=User.Role.EMPLOYEE).first()
        if not employee:
            return

        profiles = {
            cp.user.username: cp
            for cp in ConsultantProfile.objects.select_related("user").all()
        }
        jobs = {j.title: j for j in Job.objects.all()}

        submissions_data = [
            ("john_dev", "Senior Python Developer", "APPLIED"),
            ("john_dev", "DevOps Engineer - Kubernetes", "IN_PROGRESS"),
            ("alice_devops", "DevOps Engineer - Kubernetes", "INTERVIEW"),
            ("alice_devops", "Senior Python Developer", "APPLIED"),
            ("bob_data", "Data Engineer - Real-time Pipelines", "OFFER"),
            ("dave_security", "Security Engineer", "APPLIED"),
            ("carol_mobile", "React Native Developer", "INTERVIEW"),
        ]

        for uname, job_title, status in submissions_data:
            profile = profiles.get(uname)
            job = jobs.get(job_title)
            if not profile or not job:
                continue
            sub, created = ApplicationSubmission.objects.get_or_create(
                job=job, consultant=profile,
                defaults={"status": status, "submitted_by": employee},
            )
            if created:
                SubmissionStatusHistory.objects.create(
                    submission=sub, from_status="", to_status=status,
                    note="Initial submission via seed data",
                )

        # Create an offer for Bob's data engineer submission
        try:
            bob_sub = ApplicationSubmission.objects.get(
                consultant=profiles["bob_data"],
                job=jobs["Data Engineer - Real-time Pipelines"],
            )
            if not hasattr(bob_sub, "offer_detail") or bob_sub.offer_detail is None:
                try:
                    bob_sub.offer_detail
                except ApplicationSubmission.offer_detail.RelatedObjectDoesNotExist:
                    offer = Offer.objects.create(
                        submission=bob_sub,
                        initial_salary=Decimal("145000.00"),
                        initial_currency="USD",
                        initial_notes="Base offer from DataFlow Inc",
                    )
                    OfferRound.objects.create(
                        offer=offer, round_number=1,
                        salary=Decimal("145000.00"), currency="USD",
                        notes="Initial offer",
                    )
                    OfferRound.objects.create(
                        offer=offer, round_number=2,
                        salary=Decimal("155000.00"), currency="USD",
                        bonus_notes="$10k signing bonus",
                        notes="Counter-offer accepted",
                    )
        except (ApplicationSubmission.DoesNotExist, KeyError):
            pass

        self.stdout.write(self.style.SUCCESS("  Created 7 submissions with status history and offer negotiation"))

    # ── Interviews ────────────────────────────────────────────────────
    def _seed_interviews(self):
        from interviews_app.models import Interview
        from submissions.models import ApplicationSubmission

        now = timezone.now()

        interview_subs = ApplicationSubmission.objects.filter(
            status__in=["INTERVIEW", "OFFER"]
        ).select_related("consultant", "job")

        for sub in interview_subs:
            if not Interview.objects.filter(submission=sub).exists():
                Interview.objects.create(
                    submission=sub,
                    consultant=sub.consultant,
                    job_title=sub.job.title,
                    company=sub.job.company,
                    scheduled_at=now + timedelta(days=3),
                    round=Interview.Round.SCREENING,
                    status=Interview.Status.SCHEDULED,
                    notes=f"Initial screening for {sub.job.title}",
                )
                if sub.status == "OFFER":
                    Interview.objects.create(
                        submission=sub,
                        consultant=sub.consultant,
                        job_title=sub.job.title,
                        company=sub.job.company,
                        scheduled_at=now - timedelta(days=7),
                        round=Interview.Round.TECHNICAL,
                        status=Interview.Status.COMPLETED,
                        notes="Technical deep-dive completed successfully",
                    )
        self.stdout.write(self.style.SUCCESS("  Created interviews"))

    # ── Prompts ───────────────────────────────────────────────────────
    def _seed_prompts(self):
        from prompts_app.models import Prompt

        admin = User.objects.filter(is_superuser=True).first()

        prompts_data = [
            {
                "name": "Standard Resume Generator",
                "description": "Default prompt for ATS-optimized resume generation",
                "system_text": (
                    "You are an expert resume writer specializing in ATS-optimized resumes "
                    "for tech professionals. Write clear, quantified achievements. "
                    "Use action verbs. Match keywords from the job description."
                ),
                "template_text": (
                    "Generate a professional resume for the following candidate and job:\n\n"
                    "CANDIDATE:\n{candidate_info}\n\n"
                    "JOB DESCRIPTION:\n{job_description}\n\n"
                    "Requirements:\n"
                    "- ATS-friendly formatting\n"
                    "- Quantified achievements where possible\n"
                    "- Match keywords from the JD\n"
                    "- Professional summary tailored to this role"
                ),
                "is_default": True,
                "is_active": True,
            },
            {
                "name": "Executive Resume",
                "description": "For senior/leadership positions with emphasis on impact",
                "system_text": (
                    "You are a senior executive resume writer. Focus on leadership, "
                    "strategic impact, P&L ownership, and organizational transformation."
                ),
                "template_text": (
                    "Create an executive-level resume:\n\n"
                    "CANDIDATE:\n{candidate_info}\n\n"
                    "TARGET ROLE:\n{job_description}\n\n"
                    "Focus on: leadership scope, revenue impact, team size, strategic initiatives."
                ),
                "is_default": False,
                "is_active": True,
            },
        ]
        for pd in prompts_data:
            Prompt.objects.get_or_create(name=pd["name"], defaults={**pd, "created_by": admin})
        self.stdout.write(self.style.SUCCESS("  Created prompt templates"))

    # ── Messages ──────────────────────────────────────────────────────
    def _seed_messages(self):
        from messaging.models import Thread, Message

        users = {u.username: u for u in User.objects.all()}
        sarah = users.get("sarah_hr")
        john = users.get("john_dev")
        alice = users.get("alice_devops")

        if not sarah or not john:
            return
        if Thread.objects.exists():
            return

        # Thread 1: Sarah <-> John about Python Dev role
        t1 = Thread.objects.create()
        t1.participants.add(sarah, john)
        Message.objects.create(
            thread=t1, sender=sarah,
            content="Hi John! I've submitted your profile for the Senior Python Developer role at TechCorp. Let me know if you have questions.",
        )
        Message.objects.create(
            thread=t1, sender=john,
            content="Thanks Sarah! I saw the JD - looks like a great fit. When should I expect to hear back?",
        )
        Message.objects.create(
            thread=t1, sender=sarah,
            content="Usually within 5 business days. I'll keep you posted!",
            is_read=False,
        )

        # Thread 2: Sarah <-> Alice about DevOps role
        if alice:
            t2 = Thread.objects.create()
            t2.participants.add(sarah, alice)
            Message.objects.create(
                thread=t2, sender=sarah,
                content="Alice, CloudNine wants to schedule a screening interview for the DevOps role. Are you available next week?",
            )
            Message.objects.create(
                thread=t2, sender=alice,
                content="Yes! I'm free Tuesday and Thursday afternoon. Either works for me.",
            )

        self.stdout.write(self.style.SUCCESS("  Created message threads"))

    # ── Placements, Timesheets, Commissions ──────────────────────────
    def _seed_placements(self):
        from submissions.models import (
            ApplicationSubmission, Placement, Timesheet, Commission,
            record_submission_status_change,
        )
        from users.models import ConsultantProfile
        from datetime import date

        if Placement.objects.exists():
            return

        employee = User.objects.filter(role=User.Role.EMPLOYEE).first()
        if not employee:
            return

        profiles = {
            cp.user.username: cp
            for cp in ConsultantProfile.objects.select_related("user").all()
        }

        # 1. Place Bob (Data Engineer) — contract placement
        try:
            bob_sub = ApplicationSubmission.objects.get(
                consultant=profiles.get("bob_data"),
                job__title="Data Engineer - Real-time Pipelines",
            )
            old_status = bob_sub.status
            bob_sub.status = ApplicationSubmission.Status.PLACED
            bob_sub.save(update_fields=["status", "updated_at"])
            record_submission_status_change(bob_sub, "PLACED", from_status=old_status, note="Placed via seed data")

            p1 = Placement.objects.create(
                submission=bob_sub,
                placement_type=Placement.PlacementType.CONTRACT,
                status=Placement.PlacementStatus.ACTIVE,
                start_date=date(2026, 3, 15),
                end_date=date(2026, 9, 15),
                bill_rate=Decimal("125.00"),
                pay_rate=Decimal("90.00"),
                currency="USD",
                notes="6-month contract with option to extend. Data pipeline modernization project.",
                created_by=employee,
            )

            # Add timesheets for the last 3 weeks
            for weeks_ago in range(3):
                week_end = date(2026, 3, 29) - timedelta(weeks=weeks_ago)
                status = "APPROVED" if weeks_ago > 0 else "SUBMITTED"
                ts = Timesheet.objects.create(
                    placement=p1,
                    week_ending=week_end,
                    hours_worked=Decimal("40.00"),
                    overtime_hours=Decimal("2.00") if weeks_ago == 1 else Decimal("0.00"),
                    status=status,
                    submitted_by=employee,
                    approved_by=employee if status == "APPROVED" else None,
                    approved_at=timezone.now() if status == "APPROVED" else None,
                )

            # Add commission for employee
            Commission.objects.create(
                placement=p1,
                employee=employee,
                commission_rate=Decimal("10.00"),
                commission_amount=Decimal("4200.00"),
                currency="USD",
                status=Commission.CommissionStatus.PENDING,
                notes="10% of first 12 weeks spread ($35/hr x 40hrs x 12wks = $16,800 x 25%)",
            )

            self.stdout.write(self.style.SUCCESS("  Created contract placement for Bob (Data Engineer)"))
        except (ApplicationSubmission.DoesNotExist, KeyError):
            self.stdout.write(self.style.WARNING("  Skipped Bob placement (submission not found)"))

        # 2. Place Carol (React Native) — permanent placement
        try:
            carol_sub = ApplicationSubmission.objects.get(
                consultant=profiles.get("carol_mobile"),
                job__title="React Native Developer",
            )
            old_status = carol_sub.status
            carol_sub.status = ApplicationSubmission.Status.PLACED
            carol_sub.save(update_fields=["status", "updated_at"])
            record_submission_status_change(carol_sub, "PLACED", from_status=old_status, note="Placed via seed data")

            p2 = Placement.objects.create(
                submission=carol_sub,
                placement_type=Placement.PlacementType.PERMANENT,
                status=Placement.PlacementStatus.ACTIVE,
                start_date=date(2026, 4, 1),
                annual_salary=Decimal("130000.00"),
                fee_percentage=Decimal("20.00"),
                fee_amount=Decimal("26000.00"),
                currency="USD",
                notes="Permanent hire. 20% placement fee on $130k salary.",
                created_by=employee,
            )

            # Commission for the permanent placement
            Commission.objects.create(
                placement=p2,
                employee=employee,
                commission_rate=Decimal("15.00"),
                commission_amount=Decimal("3900.00"),
                currency="USD",
                status=Commission.CommissionStatus.APPROVED,
                notes="15% of $26,000 placement fee",
            )

            self.stdout.write(self.style.SUCCESS("  Created permanent placement for Carol (React Native)"))
        except (ApplicationSubmission.DoesNotExist, KeyError):
            self.stdout.write(self.style.WARNING("  Skipped Carol placement (submission not found)"))

        self.stdout.write(self.style.SUCCESS("  Created placements, timesheets, and commissions"))
