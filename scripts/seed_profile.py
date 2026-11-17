"""Create the initial candidate profile (from the project brief).

Usage: uv run python -m scripts.seed_profile [--force]
Without --force an existing profile is left untouched.
"""

from __future__ import annotations

import argparse
import asyncio

from app.ai.skills import skill_category
from app.config import get_settings
from app.database.store import JsonStore
from app.schemas.profile import ProfileIn, SkillIn
from app.services.profile_service import ProfileService

SKILLS = {
    "backend": [
        "PHP",
        "Laravel",
        "Python",
        "FastAPI",
        "Flask",
        "REST API",
        "Microservices",
        "SaaS",
        "Multi-Tenant Architecture",
        "API Development",
    ],
    "frontend": ["JavaScript", "React", "Next.js", "Vue.js", "Vuex", "Redux"],
    "database": ["MySQL", "PostgreSQL", "Redis"],
    "devops": [
        "Docker",
        "AWS",
        "Jenkins",
        "GitHub Actions",
        "GitLab CI/CD",
        "CI/CD",
        "Ansible",
        "Git",
        "Linux",
    ],
    "other": ["RabbitMQ", "Web Scraping", "AI Integration", "System Architecture"],
}

PROFILE = ProfileIn(
    name="Md. Mashud Rana",
    headline="Senior Full Stack Developer (PHP/Laravel, Python)",
    years_experience=5,
    target_titles=[
        "Senior Full Stack Developer",
        "Senior PHP Developer",
        "Senior Laravel Developer",
        "Senior Backend Developer",
        "PHP Team Lead",
        "Software Engineer",
        "Python Backend Developer",
    ],
    preferred_locations=["Bangladesh", "Dhaka", "Worldwide Remote", "Hybrid", "International"],
    job_types=["Full-time", "Contract"],
    summary=(
        "Full stack engineer with 5+ years building SaaS and multi-tenant platforms with PHP/Laravel and "
        "Python (FastAPI/Flask), React/Next.js and Vue.js front ends, MySQL/PostgreSQL/Redis, RabbitMQ, "
        "Docker, AWS and CI/CD pipelines (Jenkins, GitHub Actions, GitLab CI)."
    ),
    skills=[SkillIn(name=n, category=skill_category(n) or cat) for cat, names in SKILLS.items() for n in names],
)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="overwrite the existing profile")
    args = parser.parse_args()
    settings = get_settings()
    db = JsonStore(settings)
    await db.load()
    try:
        service = ProfileService(db, [], settings.uploads_dir)
        existing = await service.get()
        if existing and not args.force:
            print(f"Profile already exists (id={existing.id}, verified={existing.is_verified}); use --force.")
            return
        profile = await service.save_manual(PROFILE)
        print(f"Saved profile id={profile.id} version={profile.version} with {len(profile.skills)} skills.")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
