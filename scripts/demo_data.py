"""Fill an EMPTY database with fictional demo data (for screenshots and UI work).

    DATA_DIR=/tmp/demo python -m scripts.demo_data

Refuses to run if the database already contains applications.
"""
from datetime import timedelta

from sqlmodel import func, select

from app.db import get_profile, init_db, session_scope
from app.models import Application, EmailEvent, Job, utcnow

JOBS = [
    ("Junior DevOps Engineer", "Northwind Kft.", "Budapest, Hungary", False, "HU", "hu", 88,
     "Erős Docker- és Linux-alapok, CI/CD tapasztalat a projektekből. Hiányzik: Kubernetes élesben.", "karrier@northwind.example"),
    ("Platform Engineer (Junior)", "Contoso Cloud", "Remote, EU", True, "", "en", 81,
     "Python automation and GitHub Actions match well; Terraform is listed as nice-to-have.", ""),
    ("Rendszerüzemeltető", "Fabrikam Zrt.", "Győr, Hungary", False, "HU", "hu", 74,
     "Windows Server és hálózati ismeretek egyeznek; a 24/7 ügyelet nem szerepel a preferenciáid között.", "hr@fabrikam.example"),
    ("Site Reliability Intern", "Tailspin Toys", "Remote", True, "", "en", 62,
     "Good monitoring basics; the role expects Go, which isn't on your CV.", ""),
]

APPS = [
    ("Adventure Works", "Junior Cloud Engineer", "interview", 12, "Kovács Anna", "+36 30 123 4567", "bruttó 750 000 Ft/hó"),
    ("Litware Hungary", "DevOps Trainee", "received", 6, "", "", "bruttó 650 000 Ft/hó"),
    ("Proseware", "Junior SRE", "applied", 2, "", "", "€38,000/year"),
    ("Wide World Importers", "IT Operations Analyst", "rejected", 20, "Nagy Péter", "+36 1 555 0100", "bruttó 700 000 Ft/hó"),
    ("Woodgrove Bank", "Infrastructure Graduate", "offer", 25, "Szabó Eszter", "+36 20 987 6543", "bruttó 820 000 Ft/hó"),
]


def main() -> None:
    init_db()
    with session_scope() as s:
        if s.exec(select(func.count()).select_from(Application)).one():
            raise SystemExit("Database is not empty; refusing to add demo data.")
        p = get_profile(s)
        p.full_name, p.location, p.phone = "Demo Felhasználó", "Budapest", "+36 30 000 0000"
        p.search_keywords = "Junior DevOps, Platform Engineer, Cloud Engineer, System Administrator"
        p.salary_huf, p.salary_eur, p.cv_filename, p.cv_text = "bruttó 700 000 Ft/hó", "€40,000/year", "demo_cv.pdf", "Demo CV"
        s.add(p)
        now = utcnow()
        for i, (title, co, loc, remote, country, lang, score, reason, email) in enumerate(JOBS):
            s.add(Job(source="demo", external_id=f"demo-{i}", title=title, company=co, location=loc, is_remote=remote,
                      country=country, language=lang, score=score, score_reason=reason, apply_email=email,
                      status="suggested", url="https://example.com/job", posted_at=now - timedelta(days=i + 1),
                      description="Fictional demo posting.\n\nResponsibilities: ...\nRequirements: ..."))
        draft_job = Job(source="demo", external_id="demo-draft", title="Junior Linux Administrator", company="Coho Winery",
                        location="Budapest, Hungary", country="HU", language="hu", score=79, status="drafted",
                        score_reason="Linux és shell scripting erős egyezés.", apply_email="allas@coho.example",
                        url="https://example.com/job", description="Fictional demo posting.")
        s.add(draft_job)
        s.commit()
        s.refresh(draft_job)
        s.add(Application(job_id=draft_job.id, company="Coho Winery", position="Junior Linux Administrator",
                          job_url="https://example.com/job", language="hu", contact_email="allas@coho.example",
                          salary_expectation="bruttó 700 000 Ft/hó", method="email",
                          cover_letter_subject="Jelentkezés: Junior Linux Administrator",
                          cover_letter="Tisztelt Coho Winery Csapat!\n\nNagy érdeklődéssel olvastam a Junior Linux Administrator "
                                       "pozícióról szóló hirdetésüket...\n\nÜdvözlettel:\nDemo Felhasználó\n+36 30 000 0000"))
        for co, pos, st, days, cname, phone, sal in APPS:
            a = Application(company=co, position=pos, status=st, method="email", applied_at=now - timedelta(days=days),
                            updated_at=now - timedelta(days=max(0, days - 3)), contact_name=cname, contact_phone=phone,
                            contact_email=f"hr@{co.split()[0].lower()}.example", salary_expectation=sal,
                            last_email_at=now - timedelta(days=max(0, days - 3)) if st != "applied" else None)
            s.add(a)
            s.commit()
            s.refresh(a)
            if st != "applied":
                s.add(EmailEvent(gmail_id=f"demo-{a.id}", application_id=a.id, from_addr=f"{cname or 'HR'} <{a.contact_email}>",
                                 subject=f"Re: {pos}", snippet="Köszönjük a jelentkezését...", detected_status=st,
                                 received_at=a.last_email_at))
        s.commit()
    print("Demo data added.")


if __name__ == "__main__":
    main()
