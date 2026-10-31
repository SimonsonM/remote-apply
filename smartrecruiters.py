"""SmartRecruiters application adapter, enabled only by smartrecruiters_auto."""
import ats_form


def _fill_page(page, job, prof, bank):
    return ats_form.fill_page(page, job, prof, bank)


def apply_job(page, job, prof, bank, dry=False):
    return ats_form.apply_job(page, job, prof, bank, dry=dry)
