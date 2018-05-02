"""etesync_server URL Configuration

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/2.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.urls import include, path, re_path
from django.contrib import admin

from rest_framework_nested import routers
from rest_framework.authtoken import views as token_views

from journal import views

router = routers.DefaultRouter()
router.register(r'journals', views.JournalViewSet)
router.register(r'journal/(?P<journal_uid>[^/]+)', views.EntryViewSet)
router.register(r'user', views.UserInfoViewSet)

journals_router = routers.NestedSimpleRouter(router, r'journals', lookup='journal')
journals_router.register(r'members', views.MembersViewSet, base_name='journal-members')
journals_router.register(r'entries', views.EntryViewSet, base_name='journal-entries')


urlpatterns = [
    re_path(r'^api/v1/', include(router.urls)),
    re_path(r'^api/v1/', include(journals_router.urls)),
    re_path(r'^api-auth/', include('rest_framework.urls', namespace='rest_framework')),
    re_path(r'^api-token-auth/', token_views.obtain_auth_token),
    path('admin/', admin.site.urls),
]
