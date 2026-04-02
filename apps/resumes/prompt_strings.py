DEFAULT_SYSTEM_PROMPT = (
    "You are a professional resume writer specializing in consulting and IT staffing. "
    "Generate a polished, ATS-optimized resume tailored to the specific job description. "
    "Use plain text only with these sections (uppercase headings):\n"
    "PROFESSIONAL SUMMARY\n"
    "SKILLS\n"
    "PROFESSIONAL EXPERIENCE\n"
    "EDUCATION\n"
    "CERTIFICATIONS (only if provided)\n\n"
    "Be specific, quantify achievements where possible, and align the resume language "
    "with the job description keywords."
)

REQUIRED_TERMS_SYSTEM_PROMPT = (
    "You extract required ATS terms from job descriptions. "
    "Return ONLY a JSON array of short phrases (2–4 words max), no prose."
)

REQUIRED_TERMS_USER_PROMPT = (
    "Extract the required technical terms and responsibilities from this JD. "
    "Include skills, tools, systems, and operational responsibilities. "
    "Do NOT include company name, salary, benefits, location, or dates. "
    "Return ONLY a JSON array of terms.\n\n"
    "JOB DESCRIPTION:\n"
    "{jd_text}\n"
)


SUMMARY_SYSTEM_PROMPT = (
    "You are a resume writer. Output ONLY one paragraph for PROFESSIONAL SUMMARY, "
    "70–80 words, no bullets, no line breaks."
)

SUMMARY_USER_PROMPT = (
    "Write a PROFESSIONAL SUMMARY following these rules:\n"
    "- Single paragraph, 70–80 words.\n"
    "- The summary must start with the exact job title from the JD.\n"
    "- Include \"{years_display} years\" exactly.\n"
    "- Use at least 3–4 JD keywords (exact terms).\n"
    "- No pronouns, no company names, no buzzwords.\n"
    "- Do NOT use generic phrases like 'innovative solutions' or 'proven track record'.\n"
    "- Do NOT use the words 'led' or 'mentor' or 'mentored' in the summary; use neutral collaboration verbs instead.\n"
    "- Do NOT add seniority words unless they are in the exact job title.\n"
    "- Do NOT mention salary, compensation, posting date, or location details.\n"
    "- Do NOT mention hybrid/onsite/remote/work arrangement.\n"
    "- Do NOT use the phrase 'vision' or 'contributing to the vision'.\n"
    "- Active voice, confident but grounded.\n"
    "- Mention collaboration with cross-functional teams.\n"
    "- Use a measurable outcome if available.\n"
    "\nJD KEYWORDS:\n"
    "{jd_keywords}\n"
    "\nEXPERIENCE SUMMARY:\n"
    "{exp_summary_text}\n"
    "\nAVAILABLE METRICS:\n"
    "{metrics}\n"
    "\nJOB DESCRIPTION:\n"
    "{jd_text}\n"
)

SUMMARY_RETRY_SUFFIX = (
    "Include these terms exactly: {required_terms}\n"
    "Rewrite to meet all rules exactly. Do not exceed 80 words."
)

BULLETS_SYSTEM_PROMPT = "You are a resume assistant. Return only JSON, no prose."

BULLETS_PROMPT_BASE = (
    "Generate responsibilities bullets for the roles below.\n"
    "Use ONLY the job description and base resume text as sources.\n"
    "Do NOT invent companies, titles, dates, certifications, or education.\n"
    "Do NOT repeat the same sentence or phrase across bullets or roles.\n"
    "Do NOT keyword-stuff or append lists of JD terms.\n"
    "Do NOT use the words 'led' or 'mentor' or 'mentored' in any bullet.\n"
    "Avoid vague adverbs like 'significantly' or 'substantially'; use concrete outcomes or metrics instead.\n"
    "Each bullet must be 22–25 words and follow: Action + Tool/Method + Outcome.\n"
    "Return valid JSON ONLY in this format:\n"
    "{\"roles\":[{\"title\":\"\",\"company\":\"\",\"count\":0,\"bullets\":[\"...\"]}]}\n\n"
    "ROLES:\n"
    "{roles}\n\n"
    "JOB DESCRIPTION:\n"
    "{jd}\n\n"
    "BASE RESUME:\n"
    "{base_resume}\n"
)

BULLETS_PROMPT_NO_BASE = (
    "Generate responsibilities bullets for the roles below.\n"
    "There is NO base resume. You must CREATE bullets from scratch.\n\n"
    "RULES:\n"
    "- Each bullet must follow this structure: [Action Verb] + [Specific Technology/Method] + [Context/Challenge] + [Quantifiable Outcome]\n"
    "- Each bullet must be 22–25 words\n"
    "- Do NOT keyword-stuff or append lists of JD terms\n"
    "- Do NOT use the words 'led' or 'mentor' or 'mentored' in any bullet\n"
    "- Avoid vague adverbs like 'significantly' or 'substantially'; use concrete outcomes or metrics instead\n"
    "- Map JD responsibilities to realistic tasks a person in each role would perform\n"
    "- The most recent role should reflect the seniority and scope matching the JD\n"
    "- Older roles should show growth progression leading to the current level\n"
    "- Do NOT invent companies, titles, dates, certifications, or education\n"
    "- Do NOT copy JD responsibilities word-for-word; rephrase as accomplishments\n"
    "- Naturally integrate specific tools/services mentioned in the JD (e.g., EC2, Docker, Python) into sentences. Do NOT list them.\n"
    "- Do NOT repeat the same sentence or phrase across bullets or roles\n"
    "- Example: 'Engineered a scalable CI/CD pipeline using Jenkins and Docker, reducing deployment cycle times by 40% and ensuring 99.9% uptime.'\n\n"
    "METRIC RULES:\n"
    "- Most recent role: up to 2 quantified bullets if JD mentions KPIs/SLA/performance\n"
    "- Older roles: max 1 quantified bullet each\n"
    "- Every metric must have [Action] + [Tool] + [Result] (no orphaned numbers)\n"
    "- Mix metric types: %, $, time, scale. Do NOT repeat the same unit\n\n"
    "Return valid JSON ONLY in this format:\n"
    "{\"roles\":[{\"title\":\"\",\"company\":\"\",\"count\":0,\"bullets\":[\"...\"]}]}\n\n"
    "ROLES:\n"
    "{roles}\n\n"
    "JOB DESCRIPTION:\n"
    "{jd}\n"
)

BULLETS_REPAIR_PROMPT = (
    "Rewrite the bullets below to strictly follow:\n"
    "- 22–25 words per bullet\n"
    "- Action + Tool/Method + Outcome\n"
    "- No repeated sentences or phrases\n"
    "- Avoid vague adverbs like 'significantly' or 'substantially'; use concrete outcomes or metrics instead\n"
    "Return valid JSON ONLY in this format:\n"
    "{\"roles\":[{\"title\":\"\",\"company\":\"\",\"count\":0,\"bullets\":[\"...\"]}]}\n\n"
    "ROLES:\n"
    "{roles}\n\n"
    "CURRENT BULLETS:\n"
    "{bullets_map}\n\n"
    "JOB DESCRIPTION:\n"
    "{jd}\n"
)

BULLETS_MISSING_TERMS_PROMPT = (
    "Rewrite the bullets below to include the missing JD terms exactly, without changing titles/companies.\n"
    "Rules:\n"
    "- Keep 7–10 bullets for most recent role; 6 for others.\n"
    "- Each bullet 22–25 words.\n"
    "- Do NOT use 'led' or 'mentor'.\n"
    "- Missing terms to include: {missing_terms}\n"
    "Return valid JSON ONLY in this format:\n"
    "{\"roles\":[{\"title\":\"\",\"company\":\"\",\"bullets\":[\"...\"]}]}\n\n"
    "CURRENT BULLETS:\n"
    "{roles_payload}\n"
)

BUILD_PROMPT_SUMMARY_INSTRUCTION = "PROFESSIONAL SUMMARY: Generate a concise summary using the prompt rules and the inputs below."
BUILD_PROMPT_SKILLS_WITH_PROFILE = "SKILLS (key:value lines, no bullets). Example: Cloud Platforms: AWS (EC2, S3), Azure."
BUILD_PROMPT_SKILLS_GENERATE = "SKILLS: Generate key:value lines based on JD and consultant profile (no bullets)."
BUILD_PROMPT_BASE_RESUME_LABEL = "Base Resume:"
BUILD_PROMPT_BASE_RESUME_MISSING = (
    "Base Resume: NOT PROVIDED — Generate all content from scratch."
)
BUILD_PROMPT_BASE_RESUME_GUIDE_1 = (
    "Use the JD responsibilities, required qualifications, and the consultant's role titles/companies/dates to create realistic, relevant experience bullets."
)
BUILD_PROMPT_BASE_RESUME_GUIDE_2 = (
    "Each bullet must follow: [Action Verb] + [Specific Technology/Method] + [Business Outcome]."
)
BUILD_PROMPT_BASE_RESUME_GUIDE_3 = (
    "Do NOT copy JD language word-for-word; rephrase as personal accomplishments."
)
BUILD_PROMPT_EXPERIENCE_LABEL = "PROFESSIONAL EXPERIENCE:"
BUILD_PROMPT_NO_EXPERIENCE = "- No experience listed."
BUILD_PROMPT_EDUCATION_LABEL = "EDUCATION:"
BUILD_PROMPT_NO_EDUCATION = "- No education listed."
BUILD_PROMPT_JD_HEADER = "--- JOB DESCRIPTION ---"
BUILD_PROMPT_ROLE_NO_DESC = (
    "Responsibilities: Generate bullets from scratch using the JD."
    " The first role listed is most recent and needs 7–10 bullets; all other roles need exactly 6."
    " Each bullet: [Action Verb] + [Technology/Tool] + [Context] + [Outcome], 22–25 words. Do NOT copy JD text verbatim."
    " Do NOT use the words 'led' or 'mentor' or 'mentored'."
    " Keep the role title, company, and dates exactly as provided."
)
BUILD_PROMPT_TEMPLATE_SUFFIX = (
    "--- JOB DESCRIPTION ---\n"
    "{jd_text}\n"
)
BUILD_PROMPT_BASE_SECTION_WITH = (
    "Base Resume:\n"
    "{base_resume_text}\n"
)
BUILD_PROMPT_BASE_SECTION_WITHOUT = (
    "Base Resume: NOT PROVIDED — Generate all content from scratch using the JD.\n"
    "Each bullet: [Action Verb] + [Technology/Tool] + [Outcome].\n"
)
BUILD_PROMPT_REQUIRED_SECTIONS = (
    "Required Resume Sections: PROFESSIONAL SUMMARY, SKILLS, PROFESSIONAL EXPERIENCE, EDUCATION\n"
)

JD_PARSER_SYSTEM_PROMPT = (
    "You are an expert JD analyzer for resume optimization. "
    "Extract structured data from ANY Job Description for ATS resume generation. "
    "Return ONLY valid JSON. No explanation. No markdown."
)

JD_PARSER_USER_PROMPT = (
    "First, identify the ROLE DOMAIN from this list:\n"
    "- DevOps / Cloud / Infrastructure\n"
    "- Data Engineering / ETL / Pipeline\n"
    "- Data Science / ML / AI\n"
    "- Software Engineering / Full Stack / Backend / Frontend\n"
    "- Database Administration (DBA)\n"
    "- JIRA Administration / Atlassian Tools\n"
    "- ServiceNow Administration / ITSM\n"
    "- Business Intelligence / Analytics\n"
    "- Cybersecurity / InfoSec\n"
    "- Project Management / Scrum Master\n"
    "- Network Engineering\n"
    "- QA / Test Engineering\n"
    "- Mobile Development\n"
    "- Other: [detect and name it]\n"
    "\nThen extract ALL of the following as JSON:\n"
    "{\n"
    "  \"job_title\": \"\",\n"
    "  \"company\": \"\",\n"
    "  \"role_domain\": \"\",\n"
    "  \"seniority_level\": \"entry/mid/senior/lead/principal\",\n"
    "  \"required_skills\": [],\n"
    "  \"preferred_skills\": [],\n"
    "  \"exact_phrases\": [],\n"
    "  \"action_verbs\": [],\n"
    "  \"responsibilities\": [],\n"
    "  \"soft_skills\": [],\n"
    "  \"tools_and_technologies\": [],\n"
    "  \"platforms_and_services\": [],\n"
    "  \"domain_specific_terms\": [],\n"
    "  \"certifications_preferred\": [],\n"
    "  \"keywords_for_ats\": []\n"
    "}\n"
    "\nRULES:\n"
    "- Preserve EXACT spelling and casing from JD\n"
    "- Do not invent anything not in the JD\n"
    "- Empty field → return []\n"
    "- Return ONLY valid JSON. No explanation. No markdown.\n"
    "\nJOB DESCRIPTION:\n"
    "{jd_text}\n"
)
