# Copyright © 2017 Tom Hacohen
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, version 3.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

from rest_framework import permissions
from django_etebase.models import Collection, AccessLevels


def is_collection_admin(collection, user):
    member = collection.members.filter(user=user).first()
    return (member is not None) and (member.accessLevel == AccessLevels.ADMIN)


class IsCollectionAdmin(permissions.BasePermission):
    """
    Custom permission to only allow owners of a collection to view it
    """

    message = {
        "detail": "Only collection admins can perform this operation.",
        "code": "admin_access_required",
    }

    def has_permission(self, request, view):
        collection_uid = view.kwargs["collection_uid"]
        try:
            collection = view.get_collection_queryset().get(main_item__uid=collection_uid)
            return is_collection_admin(collection, request.user)
        except Collection.DoesNotExist:
            # If the collection does not exist, we want to 404 later, not permission denied.
            return True


class IsCollectionAdminOrReadOnly(permissions.BasePermission):
    """
    Custom permission to only allow owners of a collection to edit it
    """

    message = {
        "detail": "Only collection admins can edit collections.",
        "code": "admin_access_required",
    }

    def has_permission(self, request, view):
        collection_uid = view.kwargs.get("collection_uid", None)

        # Allow creating new collections
        if collection_uid is None:
            return True

        try:
            collection = view.get_collection_queryset().get(main_item__uid=collection_uid)
            if request.method in permissions.SAFE_METHODS:
                return True

            return is_collection_admin(collection, request.user)
        except Collection.DoesNotExist:
            # If the collection does not exist, we want to 404 later, not permission denied.
            return True


class HasWriteAccessOrReadOnly(permissions.BasePermission):
    """
    Custom permission to restrict write
    """

    message = {
        "detail": "You need write access to write to this collection",
        "code": "no_write_access",
    }

    def has_permission(self, request, view):
        collection_uid = view.kwargs["collection_uid"]
        try:
            collection = view.get_collection_queryset().get(main_item__uid=collection_uid)
            if request.method in permissions.SAFE_METHODS:
                return True
            else:
                member = collection.members.get(user=request.user)
                return member.accessLevel != AccessLevels.READ_ONLY
        except Collection.DoesNotExist:
            # If the collection does not exist, we want to 404 later, not permission denied.
            return True
