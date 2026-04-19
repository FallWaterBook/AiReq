from django.contrib import admin
from django.urls import path
from django.views.generic import RedirectView

from jobs.views import (
    git_diff_view,
    job_commit_view,
    job_detail_view,
    job_push_view,
    job_test_view,
    jobs_view,
)

urlpatterns = [
    path("admin/", admin.site.urls),
    path("jobs", jobs_view, name="jobs"),
    path("jobs/", jobs_view),
    path("jobs/<int:job_id>", job_detail_view, name="job-detail"),
    path("jobs/<int:job_id>/", job_detail_view),
    path("jobs/<int:job_id>/test", job_test_view, name="job-test"),
    path("jobs/<int:job_id>/test/", job_test_view),
    path("jobs/<int:job_id>/commit", job_commit_view, name="job-commit"),
    path("jobs/<int:job_id>/commit/", job_commit_view),
    path("jobs/<int:job_id>/push", job_push_view, name="job-push"),
    path("jobs/<int:job_id>/push/", job_push_view),
    path("git-diff", git_diff_view, name="git-diff"),
    path("git-diff/", git_diff_view),
    path("", RedirectView.as_view(url="/jobs", permanent=False)),
]
