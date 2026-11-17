"""Technology vocabulary with aliases, used for deterministic skill detection.

Detection is regex-based on word boundaries so "Go" doesn't match "Google" and "Java" doesn't match
"JavaScript". Used both to pre-score jobs and to verify AI output (no invented skills).
"""

from __future__ import annotations

import re
from functools import lru_cache

# canonical name -> (category, aliases)
VOCAB: dict[str, tuple[str, tuple[str, ...]]] = {
    # backend
    "PHP": ("backend", ("php", "php8", "php 8", "php7")),
    "Laravel": ("backend", ("laravel", "lumen", "eloquent")),
    "Symfony": ("backend", ("symfony",)),
    "CodeIgniter": ("backend", ("codeigniter",)),
    "WordPress": ("backend", ("wordpress",)),
    "Python": ("backend", ("python", "python3")),
    "FastAPI": ("backend", ("fastapi", "fast api")),
    "Flask": ("backend", ("flask",)),
    "Django": ("backend", ("django",)),
    "Node.js": ("backend", ("node.js", "nodejs", "node js")),
    "Express": ("backend", ("express.js", "expressjs")),
    "NestJS": ("backend", ("nestjs", "nest.js")),
    "Go": ("backend", ("golang", "go lang")),
    "Java": ("backend", ("java",)),
    "Spring": ("backend", ("spring boot", "springboot")),
    "Ruby on Rails": ("backend", ("ruby on rails", "rails", "ror")),
    "Ruby": ("backend", ("ruby",)),
    ".NET": ("backend", (".net", "dotnet", "asp.net")),
    "C#": ("backend", ("c#", "csharp")),
    "Rust": ("backend", ("rust",)),
    "Elixir": ("backend", ("elixir",)),
    "Kotlin": ("backend", ("kotlin",)),
    "Scala": ("backend", ("scala",)),
    "REST API": ("backend", ("rest api", "restful", "rest apis", "rest")),
    "GraphQL": ("backend", ("graphql",)),
    "gRPC": ("backend", ("grpc",)),
    "Microservices": ("architecture", ("microservices", "microservice", "micro-services")),
    "SaaS": ("architecture", ("saas",)),
    "Multi-Tenant Architecture": (
        "architecture",
        ("multi-tenant", "multitenant", "multi tenant", "multi-tenancy"),
    ),
    "System Architecture": (
        "architecture",
        ("system design", "system architecture", "software architecture"),
    ),
    "Event-Driven Architecture": ("architecture", ("event-driven", "event driven", "event sourcing", "cqrs")),
    # frontend
    "JavaScript": ("frontend", ("javascript", "es6", "ecmascript")),
    "TypeScript": ("frontend", ("typescript",)),
    "React": ("frontend", ("react", "react.js", "reactjs")),
    "Next.js": ("frontend", ("next.js", "nextjs")),
    "Vue.js": ("frontend", ("vue", "vue.js", "vuejs", "vue 3", "nuxt", "nuxt.js")),
    "Vuex": ("frontend", ("vuex", "pinia")),
    "Redux": ("frontend", ("redux", "rtk query", "redux toolkit")),
    "Angular": ("frontend", ("angular",)),
    "Svelte": ("frontend", ("svelte", "sveltekit")),
    "HTML/CSS": ("frontend", ("html", "css", "html5", "css3")),
    "Tailwind CSS": ("frontend", ("tailwind", "tailwindcss")),
    "Livewire": ("frontend", ("livewire",)),
    "Inertia.js": ("frontend", ("inertia", "inertia.js")),
    # database
    "MySQL": ("database", ("mysql", "mariadb")),
    "PostgreSQL": ("database", ("postgresql", "postgres", "psql")),
    "Redis": ("database", ("redis",)),
    "MongoDB": ("database", ("mongodb", "mongo")),
    "Elasticsearch": ("database", ("elasticsearch", "opensearch", "elastic search")),
    "SQL": ("database", ("sql",)),
    "DynamoDB": ("database", ("dynamodb",)),
    # devops / cloud
    "Docker": ("devops", ("docker", "docker compose", "docker-compose", "containers")),
    "Kubernetes": ("devops", ("kubernetes", "k8s", "eks", "gke", "aks")),
    "AWS": ("devops", ("aws", "amazon web services", "ec2", "s3", "lambda", "rds")),
    "GCP": ("devops", ("gcp", "google cloud")),
    "Azure": ("devops", ("azure",)),
    "Terraform": ("devops", ("terraform",)),
    "Ansible": ("devops", ("ansible",)),
    "Jenkins": ("devops", ("jenkins",)),
    "GitHub Actions": ("devops", ("github actions",)),
    "GitLab CI/CD": ("devops", ("gitlab ci", "gitlab-ci", "gitlab ci/cd")),
    "CI/CD": ("devops", ("ci/cd", "continuous integration", "continuous delivery", "ci cd")),
    "Linux": ("devops", ("linux", "ubuntu", "debian")),
    "Nginx": ("devops", ("nginx",)),
    "Git": ("devops", ("git",)),
    # messaging / other
    "RabbitMQ": ("messaging", ("rabbitmq", "amqp")),
    "Kafka": ("messaging", ("kafka",)),
    "SQS": ("messaging", ("sqs",)),
    "Web Scraping": ("other", ("web scraping", "scraping", "crawler", "crawling", "beautifulsoup", "scrapy")),
    "AI Integration": (
        "ai",
        ("openai", "llm", "llms", "generative ai", "genai", "chatgpt", "ai integration", "rag"),
    ),
    "Machine Learning": ("ai", ("machine learning", "ml engineer", "pytorch", "tensorflow", "scikit-learn")),
    "API Development": ("backend", ("api development", "api design", "apis")),
    "Unit Testing": ("quality", ("phpunit", "pytest", "unit testing", "tdd", "pest")),
    "Agile": ("process", ("agile", "scrum", "kanban")),
}

# Terms too ambiguous to search for in free text (English words / substrings). Skills whose only
# terms are ambiguous are still recognised by canonical_skill() for explicit skill lists.
_AMBIGUOUS = {
    "rest",
    "rails",
    "ror",
    "go",
    "go lang",
    "containers",
    "lambda",
    "s3",
    "rds",
    "ec2",
    "apis",
    "rag",
    "vue 3",
    "spring",
    "express",
    "ruby",
    "rust",
    "agile",
    "react",
}


@lru_cache(maxsize=1)
def _compiled() -> list[tuple[str, re.Pattern[str]]]:
    out = []
    for canonical, (_, aliases) in VOCAB.items():
        terms = [t for t in dict.fromkeys([canonical.lower(), *aliases]) if t not in _AMBIGUOUS]
        if canonical == "React":
            terms += ["react", "react native"]  # too central to skip; accept rare English false positives
        if not terms:
            continue
        alternation = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
        out.append((canonical, re.compile(rf"(?<![a-z0-9+#])(?:{alternation})(?![a-z0-9+#])", re.I)))
    return out


def canonical_skill(name: str) -> str:
    """Map a free-form skill name to the canonical vocabulary name when known."""
    low = " ".join(name.lower().split())
    for canonical, (_, aliases) in VOCAB.items():
        if low == canonical.lower() or low in aliases:
            return canonical
    return name.strip()


def skill_category(name: str) -> str | None:
    entry = VOCAB.get(canonical_skill(name))
    return entry[0] if entry else None


def detect_skills(text: str) -> list[str]:
    """Canonical skills mentioned in text, in vocabulary order."""
    if not text:
        return []
    return [name for name, pattern in _compiled() if pattern.search(text)]


def mentions(text: str, skill: str) -> bool:
    """Does text mention this (canonical or free-form) skill?"""
    canonical = canonical_skill(skill)
    for name, pattern in _compiled():
        if name == canonical:
            return bool(pattern.search(text))
    return re.search(rf"(?<![a-z0-9]){re.escape(skill.lower())}(?![a-z0-9])", text.lower()) is not None
