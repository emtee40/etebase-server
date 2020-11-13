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

import msgpack

from django.conf import settings
from django.contrib.auth import get_user_model, user_logged_in, user_logged_out
from django.core.exceptions import PermissionDenied
from django.db import transaction, IntegrityError
from django.db.models import Max, Value as V, Q
from django.db.models.functions import Coalesce, Greatest
from django.http import HttpResponseBadRequest, HttpResponse, Http404
from django.shortcuts import get_object_or_404

from rest_framework import status
from rest_framework import viewsets
from rest_framework.decorators import action as action_decorator
from rest_framework.response import Response
from rest_framework.parsers import JSONParser, FormParser, MultiPartParser
from rest_framework.renderers import BrowsableAPIRenderer
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import IsAuthenticated

import nacl.encoding
import nacl.signing
import nacl.secret
import nacl.hash

from .token_auth.models import AuthToken

from .drf_msgpack.parsers import MessagePackParser
from .drf_msgpack.renderers import MessagePackRenderer

from . import app_settings, permissions
from .renderers import JSONRenderer
from .models import (
    Collection,
    CollectionItem,
    CollectionItemRevision,
    CollectionMember,
    CollectionMemberRemoved,
    CollectionInvitation,
    Stoken,
    UserInfo,
)
from .serializers import (
    AuthenticationChangePasswordInnerSerializer,
    AuthenticationSignupSerializer,
    AuthenticationLoginChallengeSerializer,
    AuthenticationLoginSerializer,
    AuthenticationLoginInnerSerializer,
    CollectionSerializer,
    CollectionItemSerializer,
    CollectionItemBulkGetSerializer,
    CollectionItemDepSerializer,
    CollectionItemRevisionSerializer,
    CollectionItemChunkSerializer,
    CollectionListMultiSerializer,
    CollectionMemberSerializer,
    CollectionInvitationSerializer,
    InvitationAcceptSerializer,
    UserInfoPubkeySerializer,
    UserSerializer,
)
from .utils import get_user_queryset
from .exceptions import EtebaseValidationError
from .parsers import ChunkUploadParser
from .signals import user_signed_up

User = get_user_model()


def msgpack_encode(content):
    return msgpack.packb(content, use_bin_type=True)


def msgpack_decode(content):
    return msgpack.unpackb(content, raw=False)


class BaseViewSet(viewsets.ModelViewSet):
    authentication_classes = tuple(app_settings.API_AUTHENTICATORS)
    permission_classes = tuple(app_settings.API_PERMISSIONS)
    renderer_classes = [JSONRenderer, MessagePackRenderer] + ([BrowsableAPIRenderer] if settings.DEBUG else [])
    parser_classes = [JSONParser, MessagePackParser, FormParser, MultiPartParser]
    stoken_id_fields = None

    def get_serializer_class(self):
        serializer_class = self.serializer_class

        if self.request.method == "PUT":
            serializer_class = getattr(self, "serializer_update_class", serializer_class)

        return serializer_class

    def get_collection_queryset(self, queryset=Collection.objects):
        user = self.request.user
        return queryset.filter(members__user=user)

    def get_stoken_obj_id(self, request):
        return request.GET.get("stoken", None)

    def get_stoken_obj(self, request):
        stoken = self.get_stoken_obj_id(request)

        if stoken is not None:
            try:
                return Stoken.objects.get(uid=stoken)
            except Stoken.DoesNotExist:
                raise EtebaseValidationError("bad_stoken", "Invalid stoken.", status_code=status.HTTP_400_BAD_REQUEST)

        return None

    def filter_by_stoken(self, request, queryset):
        stoken_rev = self.get_stoken_obj(request)

        aggr_fields = [Coalesce(Max(field), V(0)) for field in self.stoken_id_fields]
        max_stoken = Greatest(*aggr_fields) if len(aggr_fields) > 1 else aggr_fields[0]
        queryset = queryset.annotate(max_stoken=max_stoken).order_by("max_stoken")

        if stoken_rev is not None:
            queryset = queryset.filter(max_stoken__gt=stoken_rev.id)

        return queryset, stoken_rev

    def get_queryset_stoken(self, queryset):
        maxid = -1
        for row in queryset:
            rowmaxid = getattr(row, "max_stoken") or -1
            maxid = max(maxid, rowmaxid)
        new_stoken = (maxid >= 0) and Stoken.objects.get(id=maxid)

        return new_stoken or None

    def filter_by_stoken_and_limit(self, request, queryset):
        limit = int(request.GET.get("limit", 50))

        queryset, stoken_rev = self.filter_by_stoken(request, queryset)

        result = list(queryset[: limit + 1])
        if len(result) < limit + 1:
            done = True
        else:
            done = False
            result = result[:-1]

        new_stoken_obj = self.get_queryset_stoken(result) or stoken_rev

        return result, new_stoken_obj, done

    # Change how our list works by default
    def list(self, request, collection_uid=None, *args, **kwargs):
        queryset = self.get_queryset()
        serializer = self.get_serializer(queryset, many=True)

        ret = {
            "data": serializer.data,
            "done": True,  # we always return all the items, so it's always done
        }

        return Response(ret)


class CollectionViewSet(BaseViewSet):
    allowed_methods = ["GET", "POST"]
    permission_classes = BaseViewSet.permission_classes + (permissions.IsCollectionAdminOrReadOnly,)
    queryset = Collection.objects.all()
    serializer_class = CollectionSerializer
    lookup_field = "main_item__uid"
    lookup_url_kwarg = "uid"
    stoken_id_fields = ["items__revisions__stoken__id", "members__stoken__id"]

    def get_queryset(self, queryset=None):
        if queryset is None:
            queryset = type(self).queryset
        return self.get_collection_queryset(queryset)

    def get_serializer_context(self):
        context = super().get_serializer_context()
        prefetch = self.request.query_params.get("prefetch", "auto")
        context.update({"request": self.request, "prefetch": prefetch})
        return context

    def destroy(self, request, uid=None, *args, **kwargs):
        # FIXME: implement
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def partial_update(self, request, uid=None, *args, **kwargs):
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def update(self, request, *args, **kwargs):
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(owner=self.request.user)

        return Response({}, status=status.HTTP_201_CREATED)

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        return self.list_common(request, queryset, *args, **kwargs)

    @action_decorator(detail=False, methods=["POST"])
    def list_multi(self, request, *args, **kwargs):
        serializer = CollectionListMultiSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        collection_types = serializer.validated_data["collectionTypes"]

        queryset = self.get_queryset()
        # FIXME: Remove the isnull part once we attach collection types to all objects ("collection-type-migration")
        queryset = queryset.filter(
            Q(members__collectionType__uid__in=collection_types) | Q(members__collectionType__isnull=True)
        )

        return self.list_common(request, queryset, *args, **kwargs)

    def list_common(self, request, queryset, *args, **kwargs):
        result, new_stoken_obj, done = self.filter_by_stoken_and_limit(request, queryset)
        new_stoken = new_stoken_obj and new_stoken_obj.uid

        serializer = self.get_serializer(result, many=True)

        ret = {
            "data": serializer.data,
            "stoken": new_stoken,
            "done": done,
        }

        stoken_obj = self.get_stoken_obj(request)
        if stoken_obj is not None:
            # FIXME: honour limit? (the limit should be combined for data and this because of stoken)
            remed_qs = CollectionMemberRemoved.objects.filter(user=request.user, stoken__id__gt=stoken_obj.id)
            if not ret["done"]:
                # We only filter by the new_stoken if we are not done. This is because if we are done, the new stoken
                # can point to the most recent collection change rather than most recent removed membership.
                remed_qs = remed_qs.filter(stoken__id__lte=new_stoken_obj.id)

            remed = remed_qs.values_list("collection__main_item__uid", flat=True)
            if len(remed) > 0:
                ret["removedMemberships"] = [{"uid": x} for x in remed]

        return Response(ret)


class CollectionItemViewSet(BaseViewSet):
    allowed_methods = ["GET", "POST", "PUT"]
    permission_classes = BaseViewSet.permission_classes + (permissions.HasWriteAccessOrReadOnly,)
    queryset = CollectionItem.objects.all()
    serializer_class = CollectionItemSerializer
    lookup_field = "uid"
    stoken_id_fields = ["revisions__stoken__id"]

    def get_queryset(self):
        collection_uid = self.kwargs["collection_uid"]
        try:
            collection = self.get_collection_queryset(Collection.objects).get(main_item__uid=collection_uid)
        except Collection.DoesNotExist:
            raise Http404("Collection does not exist")
        # XXX Potentially add this for performance: .prefetch_related('revisions__chunks')
        queryset = type(self).queryset.filter(collection__pk=collection.pk, revisions__current=True)

        return queryset

    def get_serializer_context(self):
        context = super().get_serializer_context()
        prefetch = self.request.query_params.get("prefetch", "auto")
        context.update({"request": self.request, "prefetch": prefetch})
        return context

    def create(self, request, collection_uid=None, *args, **kwargs):
        # We create using batch and transaction
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def destroy(self, request, collection_uid=None, uid=None, *args, **kwargs):
        # We can't have destroy because we need to get data from the user (in the body) such as hmac.
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def update(self, request, collection_uid=None, uid=None, *args, **kwargs):
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def partial_update(self, request, collection_uid=None, uid=None, *args, **kwargs):
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def list(self, request, collection_uid=None, *args, **kwargs):
        queryset = self.get_queryset()

        if not self.request.query_params.get("withCollection", False):
            queryset = queryset.filter(parent__isnull=True)

        result, new_stoken_obj, done = self.filter_by_stoken_and_limit(request, queryset)
        new_stoken = new_stoken_obj and new_stoken_obj.uid

        serializer = self.get_serializer(result, many=True)

        ret = {
            "data": serializer.data,
            "stoken": new_stoken,
            "done": done,
        }
        return Response(ret)

    @action_decorator(detail=True, methods=["GET"])
    def revision(self, request, collection_uid=None, uid=None, *args, **kwargs):
        col = get_object_or_404(self.get_collection_queryset(Collection.objects), main_item__uid=collection_uid)
        item = get_object_or_404(col.items, uid=uid)

        limit = int(request.GET.get("limit", 50))
        iterator = request.GET.get("iterator", None)

        queryset = item.revisions.order_by("-id")

        if iterator is not None:
            iterator = get_object_or_404(queryset, uid=iterator)
            queryset = queryset.filter(id__lt=iterator.id)

        result = list(queryset[: limit + 1])
        if len(result) < limit + 1:
            done = True
        else:
            done = False
            result = result[:-1]

        serializer = CollectionItemRevisionSerializer(result, context=self.get_serializer_context(), many=True)

        iterator = serializer.data[-1]["uid"] if len(result) > 0 else None

        ret = {
            "data": serializer.data,
            "iterator": iterator,
            "done": done,
        }
        return Response(ret)

    # FIXME: rename to something consistent with what the clients have - maybe list_updates?
    @action_decorator(detail=False, methods=["POST"])
    def fetch_updates(self, request, collection_uid=None, *args, **kwargs):
        queryset = self.get_queryset()

        serializer = CollectionItemBulkGetSerializer(data=request.data, many=True)
        serializer.is_valid(raise_exception=True)
        # FIXME: make configurable?
        item_limit = 200

        if len(serializer.validated_data) > item_limit:
            content = {"code": "too_many_items", "detail": "Request has too many items. Limit: {}".format(item_limit)}
            return Response(content, status=status.HTTP_400_BAD_REQUEST)

        queryset, stoken_rev = self.filter_by_stoken(request, queryset)

        uids, etags = zip(*[(item["uid"], item.get("etag")) for item in serializer.validated_data])
        revs = CollectionItemRevision.objects.filter(uid__in=etags, current=True)
        queryset = queryset.filter(uid__in=uids).exclude(revisions__in=revs)

        new_stoken_obj = self.get_queryset_stoken(queryset)
        new_stoken = new_stoken_obj and new_stoken_obj.uid
        stoken = stoken_rev and getattr(stoken_rev, "uid", None)
        new_stoken = new_stoken or stoken

        serializer = self.get_serializer(queryset, many=True)

        ret = {
            "data": serializer.data,
            "stoken": new_stoken,
            "done": True,  # we always return all the items, so it's always done
        }
        return Response(ret)

    @action_decorator(detail=False, methods=["POST"])
    def batch(self, request, collection_uid=None, *args, **kwargs):
        return self.transaction(request, collection_uid, validate_etag=False)

    @action_decorator(detail=False, methods=["POST"])
    def transaction(self, request, collection_uid=None, validate_etag=True, *args, **kwargs):
        stoken = request.GET.get("stoken", None)
        with transaction.atomic():  # We need this for locking on the collection object
            collection_object = get_object_or_404(
                self.get_collection_queryset(Collection.objects).select_for_update(),  # Lock writes on the collection
                main_item__uid=collection_uid,
            )

            if stoken is not None and stoken != collection_object.stoken:
                content = {"code": "stale_stoken", "detail": "Stoken is too old"}
                return Response(content, status=status.HTTP_409_CONFLICT)

            items = request.data.get("items")
            deps = request.data.get("deps", None)
            # FIXME: It should just be one serializer
            context = self.get_serializer_context()
            context.update({"validate_etag": validate_etag})
            serializer = self.get_serializer_class()(data=items, context=context, many=True)
            deps_serializer = CollectionItemDepSerializer(data=deps, context=context, many=True)

            ser_valid = serializer.is_valid()
            deps_ser_valid = deps is None or deps_serializer.is_valid()
            if ser_valid and deps_ser_valid:
                items = serializer.save(collection=collection_object)

                ret = {}
                return Response(ret, status=status.HTTP_200_OK)

            return Response(
                {"items": serializer.errors, "deps": deps_serializer.errors if deps is not None else [],},
                status=status.HTTP_409_CONFLICT,
            )


class CollectionItemChunkViewSet(viewsets.ViewSet):
    allowed_methods = ["GET", "PUT"]
    authentication_classes = BaseViewSet.authentication_classes
    permission_classes = BaseViewSet.permission_classes
    renderer_classes = BaseViewSet.renderer_classes
    parser_classes = (ChunkUploadParser,)
    serializer_class = CollectionItemChunkSerializer
    lookup_field = "uid"

    def get_serializer_class(self):
        return self.serializer_class

    def get_collection_queryset(self, queryset=Collection.objects):
        user = self.request.user
        return queryset.filter(members__user=user)

    def update(self, request, *args, collection_uid=None, collection_item_uid=None, uid=None, **kwargs):
        col = get_object_or_404(self.get_collection_queryset(), main_item__uid=collection_uid)
        # IGNORED FOR NOW: col_it = get_object_or_404(col.items, uid=collection_item_uid)

        data = {
            "uid": uid,
            "chunkFile": request.data["file"],
        }

        serializer = self.get_serializer_class()(data=data)
        serializer.is_valid(raise_exception=True)
        try:
            serializer.save(collection=col)
        except IntegrityError:
            return Response(
                {"code": "chunk_exists", "detail": "Chunk already exists."}, status=status.HTTP_409_CONFLICT
            )

        return Response({}, status=status.HTTP_201_CREATED)

    @action_decorator(detail=True, methods=["GET"])
    def download(self, request, collection_uid=None, collection_item_uid=None, uid=None, *args, **kwargs):
        import os
        from django.views.static import serve

        col = get_object_or_404(self.get_collection_queryset(), main_item__uid=collection_uid)
        # IGNORED FOR NOW: col_it = get_object_or_404(col.items, uid=collection_item_uid)
        chunk = get_object_or_404(col.chunks, uid=uid)

        filename = chunk.chunkFile.path
        dirname = os.path.dirname(filename)
        basename = os.path.basename(filename)

        # FIXME: DO NOT USE! Use django-send file or etc instead.
        return serve(request, basename, dirname)


class CollectionMemberViewSet(BaseViewSet):
    allowed_methods = ["GET", "PUT", "DELETE"]
    our_base_permission_classes = BaseViewSet.permission_classes
    permission_classes = our_base_permission_classes + (permissions.IsCollectionAdmin,)
    queryset = CollectionMember.objects.all()
    serializer_class = CollectionMemberSerializer
    lookup_field = f"user__{User.USERNAME_FIELD}__iexact"
    lookup_url_kwarg = "username"
    stoken_id_fields = ["stoken__id"]

    # FIXME: need to make sure that there's always an admin, and maybe also don't let an owner remove adm access
    # (if we want to transfer, we need to do that specifically)

    def get_queryset(self, queryset=None):
        collection_uid = self.kwargs["collection_uid"]
        try:
            collection = self.get_collection_queryset(Collection.objects).get(main_item__uid=collection_uid)
        except Collection.DoesNotExist:
            raise Http404("Collection does not exist")

        if queryset is None:
            queryset = type(self).queryset

        return queryset.filter(collection=collection)

    # We override this method because we expect the stoken to be called iterator
    def get_stoken_obj_id(self, request):
        return request.GET.get("iterator", None)

    def list(self, request, collection_uid=None, *args, **kwargs):
        queryset = self.get_queryset().order_by("id")
        result, new_stoken_obj, done = self.filter_by_stoken_and_limit(request, queryset)
        new_stoken = new_stoken_obj and new_stoken_obj.uid
        serializer = self.get_serializer(result, many=True)

        ret = {
            "data": serializer.data,
            "iterator": new_stoken,  # Here we call it an iterator, it's only stoken for collection/items
            "done": done,
        }

        return Response(ret)

    def create(self, request, *args, **kwargs):
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)

    # FIXME: block leaving if we are the last admins - should be deleted / assigned in this case depending if there
    # are other memebers.
    def perform_destroy(self, instance):
        instance.revoke()

    @action_decorator(detail=False, methods=["POST"], permission_classes=our_base_permission_classes)
    def leave(self, request, collection_uid=None, *args, **kwargs):
        collection_uid = self.kwargs["collection_uid"]
        col = get_object_or_404(self.get_collection_queryset(Collection.objects), main_item__uid=collection_uid)

        member = col.members.get(user=request.user)
        self.perform_destroy(member)

        return Response({})


class InvitationBaseViewSet(BaseViewSet):
    queryset = CollectionInvitation.objects.all()
    serializer_class = CollectionInvitationSerializer
    lookup_field = "uid"
    lookup_url_kwarg = "invitation_uid"

    def list(self, request, collection_uid=None, *args, **kwargs):
        limit = int(request.GET.get("limit", 50))
        iterator = request.GET.get("iterator", None)

        queryset = self.get_queryset().order_by("id")

        if iterator is not None:
            iterator = get_object_or_404(queryset, uid=iterator)
            queryset = queryset.filter(id__gt=iterator.id)

        result = list(queryset[: limit + 1])
        if len(result) < limit + 1:
            done = True
        else:
            done = False
            result = result[:-1]

        serializer = self.get_serializer(result, many=True)

        iterator = serializer.data[-1]["uid"] if len(result) > 0 else None

        ret = {
            "data": serializer.data,
            "iterator": iterator,
            "done": done,
        }

        return Response(ret)


class InvitationOutgoingViewSet(InvitationBaseViewSet):
    allowed_methods = ["GET", "POST", "PUT", "DELETE"]

    def get_queryset(self, queryset=None):
        if queryset is None:
            queryset = type(self).queryset

        return queryset.filter(fromMember__user=self.request.user)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        collection_uid = serializer.validated_data.get("collection", {}).get("uid")

        try:
            collection = self.get_collection_queryset(Collection.objects).get(main_item__uid=collection_uid)
        except Collection.DoesNotExist:
            raise Http404("Collection does not exist")

        if request.user == serializer.validated_data.get("user"):
            content = {"code": "self_invite", "detail": "Inviting yourself is invalid"}
            return Response(content, status=status.HTTP_400_BAD_REQUEST)

        if not permissions.is_collection_admin(collection, request.user):
            raise PermissionDenied(
                {"code": "admin_access_required", "detail": "User is not an admin of this collection"}
            )

        serializer.save(collection=collection)

        return Response({}, status=status.HTTP_201_CREATED)

    @action_decorator(detail=False, allowed_methods=["GET"], methods=["GET"])
    def fetch_user_profile(self, request, *args, **kwargs):
        username = request.GET.get("username")
        kwargs = {User.USERNAME_FIELD: username.lower()}
        user = get_object_or_404(get_user_queryset(User.objects.all(), self), **kwargs)
        user_info = get_object_or_404(UserInfo.objects.all(), owner=user)
        serializer = UserInfoPubkeySerializer(user_info)
        return Response(serializer.data)


class InvitationIncomingViewSet(InvitationBaseViewSet):
    allowed_methods = ["GET", "DELETE"]

    def get_queryset(self, queryset=None):
        if queryset is None:
            queryset = type(self).queryset

        return queryset.filter(user=self.request.user)

    @action_decorator(detail=True, allowed_methods=["POST"], methods=["POST"])
    def accept(self, request, invitation_uid=None, *args, **kwargs):
        invitation = get_object_or_404(self.get_queryset(), uid=invitation_uid)
        context = self.get_serializer_context()
        context.update({"invitation": invitation})

        serializer = InvitationAcceptSerializer(data=request.data, context=context)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(status=status.HTTP_201_CREATED)


class AuthenticationViewSet(viewsets.ViewSet):
    allowed_methods = ["POST"]
    authentication_classes = BaseViewSet.authentication_classes
    renderer_classes = BaseViewSet.renderer_classes
    parser_classes = BaseViewSet.parser_classes

    def get_encryption_key(self, salt):
        key = nacl.hash.blake2b(settings.SECRET_KEY.encode(), encoder=nacl.encoding.RawEncoder)
        return nacl.hash.blake2b(
            b"",
            key=key,
            salt=salt[: nacl.hash.BLAKE2B_SALTBYTES],
            person=b"etebase-auth",
            encoder=nacl.encoding.RawEncoder,
        )

    def get_queryset(self):
        return get_user_queryset(User.objects.all(), self)

    def get_serializer_context(self):
        return {"request": self.request, "format": self.format_kwarg, "view": self}

    def login_response_data(self, user):
        return {
            "token": AuthToken.objects.create(user=user).key,
            "user": UserSerializer(user).data,
        }

    def list(self, request, *args, **kwargs):
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)

    @action_decorator(detail=False, methods=["POST"])
    def signup(self, request, *args, **kwargs):
        serializer = AuthenticationSignupSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        user = serializer.save()

        user_signed_up.send(sender=user.__class__, request=request, user=user)

        data = self.login_response_data(user)
        return Response(data, status=status.HTTP_201_CREATED)

    def get_login_user(self, username):
        kwargs = {User.USERNAME_FIELD + "__iexact": username.lower()}
        try:
            user = self.get_queryset().get(**kwargs)
            if not hasattr(user, "userinfo"):
                raise AuthenticationFailed({"code": "user_not_init", "detail": "User not properly init"})
            return user
        except User.DoesNotExist:
            raise AuthenticationFailed({"code": "user_not_found", "detail": "User not found"})

    def validate_login_request(self, request, validated_data, response_raw, signature, expected_action):
        from datetime import datetime

        username = validated_data.get("username")
        user = self.get_login_user(username)
        host = validated_data["host"]
        challenge = validated_data["challenge"]
        action = validated_data["action"]

        salt = bytes(user.userinfo.salt)
        enc_key = self.get_encryption_key(salt)
        box = nacl.secret.SecretBox(enc_key)

        challenge_data = msgpack_decode(box.decrypt(challenge))
        now = int(datetime.now().timestamp())
        if action != expected_action:
            content = {"code": "wrong_action", "detail": 'Expected "{}" but got something else'.format(expected_action)}
            return Response(content, status=status.HTTP_400_BAD_REQUEST)
        elif now - challenge_data["timestamp"] > app_settings.CHALLENGE_VALID_SECONDS:
            content = {"code": "challenge_expired", "detail": "Login challange has expired"}
            return Response(content, status=status.HTTP_400_BAD_REQUEST)
        elif challenge_data["userId"] != user.id:
            content = {"code": "wrong_user", "detail": "This challenge is for the wrong user"}
            return Response(content, status=status.HTTP_400_BAD_REQUEST)
        elif not settings.DEBUG and host.split(":", 1)[0] != request.get_host():
            detail = 'Found wrong host name. Got: "{}" expected: "{}"'.format(host, request.get_host())
            content = {"code": "wrong_host", "detail": detail}
            return Response(content, status=status.HTTP_400_BAD_REQUEST)

        verify_key = nacl.signing.VerifyKey(bytes(user.userinfo.loginPubkey), encoder=nacl.encoding.RawEncoder)

        try:
            verify_key.verify(response_raw, signature)
        except nacl.exceptions.BadSignatureError:
            return Response(
                {"code": "login_bad_signature", "detail": "Wrong password for user."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        return None

    @action_decorator(detail=False, methods=["GET"])
    def is_etebase(self, request, *args, **kwargs):
        return Response({}, status=status.HTTP_200_OK)

    @action_decorator(detail=False, methods=["POST"])
    def login_challenge(self, request, *args, **kwargs):
        from datetime import datetime

        serializer = AuthenticationLoginChallengeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        username = serializer.validated_data.get("username")
        user = self.get_login_user(username)

        salt = bytes(user.userinfo.salt)
        enc_key = self.get_encryption_key(salt)
        box = nacl.secret.SecretBox(enc_key)

        challenge_data = {
            "timestamp": int(datetime.now().timestamp()),
            "userId": user.id,
        }
        challenge = box.encrypt(msgpack_encode(challenge_data), encoder=nacl.encoding.RawEncoder)

        ret = {
            "salt": salt,
            "challenge": challenge,
            "version": user.userinfo.version,
        }
        return Response(ret, status=status.HTTP_200_OK)

    @action_decorator(detail=False, methods=["POST"])
    def login(self, request, *args, **kwargs):
        outer_serializer = AuthenticationLoginSerializer(data=request.data)
        outer_serializer.is_valid(raise_exception=True)

        response_raw = outer_serializer.validated_data["response"]
        response = msgpack_decode(response_raw)
        signature = outer_serializer.validated_data["signature"]

        context = {"host": request.get_host()}
        serializer = AuthenticationLoginInnerSerializer(data=response, context=context)
        serializer.is_valid(raise_exception=True)

        bad_login_response = self.validate_login_request(
            request, serializer.validated_data, response_raw, signature, "login"
        )
        if bad_login_response is not None:
            return bad_login_response

        username = serializer.validated_data.get("username")
        user = self.get_login_user(username)

        data = self.login_response_data(user)

        user_logged_in.send(sender=user.__class__, request=request, user=user)

        return Response(data, status=status.HTTP_200_OK)

    @action_decorator(detail=False, methods=["POST"], permission_classes=[IsAuthenticated])
    def logout(self, request, *args, **kwargs):
        request.auth.delete()
        user_logged_out.send(sender=request.user.__class__, request=request, user=request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action_decorator(detail=False, methods=["POST"], permission_classes=BaseViewSet.permission_classes)
    def change_password(self, request, *args, **kwargs):
        outer_serializer = AuthenticationLoginSerializer(data=request.data)
        outer_serializer.is_valid(raise_exception=True)

        response_raw = outer_serializer.validated_data["response"]
        response = msgpack_decode(response_raw)
        signature = outer_serializer.validated_data["signature"]

        context = {"host": request.get_host()}
        serializer = AuthenticationChangePasswordInnerSerializer(request.user.userinfo, data=response, context=context)
        serializer.is_valid(raise_exception=True)

        bad_login_response = self.validate_login_request(
            request, serializer.validated_data, response_raw, signature, "changePassword"
        )
        if bad_login_response is not None:
            return bad_login_response

        serializer.save()

        return Response({}, status=status.HTTP_200_OK)

    @action_decorator(detail=False, methods=["POST"], permission_classes=[IsAuthenticated])
    def dashboard_url(self, request, *args, **kwargs):
        get_dashboard_url = app_settings.DASHBOARD_URL_FUNC
        if get_dashboard_url is None:
            raise EtebaseValidationError(
                "not_supported", "This server doesn't have a user dashboard.", status_code=status.HTTP_400_BAD_REQUEST
            )

        ret = {
            "url": get_dashboard_url(request, *args, **kwargs),
        }
        return Response(ret)


class TestAuthenticationViewSet(viewsets.ViewSet):
    allowed_methods = ["POST"]
    renderer_classes = BaseViewSet.renderer_classes
    parser_classes = BaseViewSet.parser_classes

    def get_serializer_context(self):
        return {"request": self.request, "format": self.format_kwarg, "view": self}

    def list(self, request, *args, **kwargs):
        return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)

    @action_decorator(detail=False, methods=["POST"])
    def reset(self, request, *args, **kwargs):
        # Only run when in DEBUG mode! It's only used for tests
        if not settings.DEBUG:
            return HttpResponseBadRequest("Only allowed in debug mode.")

        with transaction.atomic():
            user_queryset = get_user_queryset(User.objects.all(), self)
            user = get_object_or_404(user_queryset, username=request.data.get("user").get("username"))

            # Only allow test users for extra safety
            if not getattr(user, User.USERNAME_FIELD).startswith("test_user"):
                return HttpResponseBadRequest("Endpoint not allowed for user.")

            if hasattr(user, "userinfo"):
                user.userinfo.delete()

            serializer = AuthenticationSignupSerializer(data=request.data, context=self.get_serializer_context())
            serializer.is_valid(raise_exception=True)
            serializer.save()

            # Delete all of the journal data for this user for a clear test env
            user.collection_set.all().delete()
            user.collectionmember_set.all().delete()
            user.incoming_invitations.all().delete()

            # FIXME: also delete chunk files!!!

        return HttpResponse()
