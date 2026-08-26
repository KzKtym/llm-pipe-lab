"""
llm_pipe_lab URL Configuration

runserver で動くことを目的とした最小構成。ルートは /nl2sql/ にリダイレクトする。
"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

urlpatterns = [
    path('admin/', admin.site.urls),

    # ルートは NL→SQL 実験の閲覧画面へ
    path('', RedirectView.as_view(url='/nl2sql/', permanent=False), name='index'),

    # NL→SQL 実験の閲覧画面（読み取り専用）
    path('nl2sql/', include('app.nl2sql.web.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
