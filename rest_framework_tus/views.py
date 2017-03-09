# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import logging
import json
import os

from django.http import Http404
from django.urls.base import reverse
from django.utils import timezone
from django.utils.translation import ugettext_lazy as _

from rest_framework import mixins, status
from rest_framework.exceptions import MethodNotAllowed
from rest_framework.metadata import BaseMetadata
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from rest_framework_tus.utils import is_correct_checksum_for_file
from . import tus_api_version, tus_api_version_supported, tus_api_extensions, tus_api_checksum_algorithms, \
    settings as tus_settings, constants, signals, states

from .models import get_upload_model
from .exceptions import Conflict
from .serializers import UploadSerializer
from .utils import encode_upload_metadata, get_or_create_temp_file_for_upload, \
    write_chunk_to_temp_file, read_bytes

logger = logging.getLogger(__name__)


class UploadMetadata(BaseMetadata):
    def determine_metadata(self, request, view):
        return {
            'Tus-Resumable': tus_api_version,
            'Tus-Version': ','.join(tus_api_version_supported),
            'Tus-Extension': ','.join(tus_api_extensions),
            'Tus-Max-Size': tus_settings.TUS_MAX_FILE_SIZE,
            'Tus-Checksum-Algorithm': ','.join(tus_api_checksum_algorithms),
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'PATCH,HEAD,GET,POST,OPTIONS',
            'Access-Control-Expose-Headers': 'Tus-Resumable,upload-length,upload-metadata,Location,Upload-Offset',
            'Access-Control-Allow-Headers':
                'Tus-Resumable,upload-length,upload-metadata,Location,Upload-Offset,content-type',
            'Cache-Control': 'no-store'
        }


def add_expiry_header(upload, headers):
    if upload.expires:
        headers['Upload-Expires'] = upload.expires.strftime('%a, %d %b %Y %H:%M:%S %Z')


class TusHeadMixin(object):
    def info(self, request, *args, **kwargs):
        try:
            upload = self.get_object()
        except Http404:
            # Instead of simply trowing a 404, we need to add a cache-control header to the response
            return Response('Not found.', headers={'Cache-Control': 'no-store'}, status=status.HTTP_404_NOT_FOUND)

        headers = {
            'Upload-Offset': upload.upload_offset,
            'Cache-Control': 'no-store'
        }

        if upload.upload_length >= 0:
            headers['Upload-Length'] = upload.upload_length

        if upload.upload_metadata:
            headers['Upload-Metadata'] = encode_upload_metadata(json.loads(upload.upload_metadata))

        # Add upload expiry to headers
        add_expiry_header(upload, headers)

        return Response(headers=headers, status=status.HTTP_200_OK)


class TusCreateMixin(mixins.CreateModelMixin):
    def create(self, request, *args, **kwargs):
        # Get file size from request
        upload_length = getattr(request, constants.UPLOAD_LENGTH_FIELD_NAME, -1)

        # Validate upload_length
        if upload_length > tus_settings.TUS_MAX_FILE_SIZE:
            return Response('Invalid "Upload-Length". Maximum value: {}.'.format(tus_settings.TUS_MAX_FILE_SIZE),
                            status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)

        # If upload_length is not given, we expect the defer header!
        if not upload_length or upload_length < 0:
            if getattr(request, constants.UPLOAD_DEFER_LENGTH_FIELD_NAME, -1) != 1:
                return Response('Missing "{Upload-Defer-Length}" header.', status=status.HTTP_400_BAD_REQUEST)

        # Get metadata from request
        upload_metadata = getattr(request, constants.UPLOAD_METADATA_FIELD_NAME, {})

        # Get data from metadata
        filename = upload_metadata.get('filename', '')

        # Create upload object
        upload = get_upload_model().objects.create(
            upload_length=upload_length, upload_metadata=json.dumps(upload_metadata), filename=filename)

        # Prepare response headers
        headers = {
            'Location': reverse('rest_framework_tus:api:upload-detail', kwargs={'guid': upload.guid}),
        }

        # Maybe we're auto-expiring the upload...
        if tus_settings.TUS_UPLOAD_EXPIRES is not None:
            upload.expires = timezone.now() + tus_settings.TUS_UPLOAD_EXPIRES
            upload.save()

        # Add upload expiry to headers
        add_expiry_header(upload, headers)

        return Response(headers=headers, status=status.HTTP_201_CREATED)


class TusPatchMixin(mixins.UpdateModelMixin):
    def update(self, request, *args, **kwargs):
        raise MethodNotAllowed

    def partial_update(self, request, *args, **kwargs):
        # Validate content type
        self.validate_content_type(request)

        # Retrieve object
        upload = self.get_object()

        # Get upload_offset
        upload_offset = getattr(request, constants.UPLOAD_OFFSET_NAME)

        # Validate upload_offset
        if upload_offset != upload.upload_offset:
            raise Conflict

        # Make sure there is a tempfile for the upload
        get_or_create_temp_file_for_upload(upload)

        # Change state
        if upload.state == states.INITIAL:
            upload.start_receiving()
            upload.save()

        # Write chunk
        try:
            chunk_file = write_chunk_to_temp_file(request.body)
        except Exception as e:
            return Response(str(e), status=status.HTTP_400_BAD_REQUEST)

        # Check checksum  (http://tus.io/protocols/resumable-upload.html#checksum)
        upload_checksum = getattr(request, constants.UPLOAD_CHECKSUM_FIELD_NAME, None)
        if upload_checksum is not None:
            if upload_checksum[0] not in tus_api_checksum_algorithms:
                os.remove(chunk_file)
                return Response('Unsupported Checksum Algorithm: {}.'.format(
                    upload_checksum[0]), status=status.HTTP_400_BAD_REQUEST)
            elif not is_correct_checksum_for_file(
                upload_checksum[0], upload_checksum[1], chunk_file):
                os.remove(chunk_file)
                return Response('Checksum Mismatch.', status=460)

        # Write file
        chunk_size = int(request.META.get('CONTENT_LENGTH', 102400))
        try:
            upload.write_data(read_bytes(chunk_file), chunk_size)
        except Exception as e:
            return Response(str(e), status=status.HTTP_400_BAD_REQUEST)
        finally:
            os.remove(chunk_file)

        headers = {
            'Upload-Offset': upload.upload_offset,
        }

        if upload.upload_length == upload.upload_offset:
            # Trigger signal
            signals.received.send(sender=upload.__class__, instance=upload)

        # Add upload expiry to headers
        add_expiry_header(upload, headers)

        return Response(headers=headers, status=status.HTTP_204_NO_CONTENT)

    @classmethod
    def validate_content_type(cls, request):
        content_type = request.META.get('headers', {}).get('Content-Type', '')

        if not content_type or content_type != 'application/upload_offset+octet-stream':
            return Response(
                'Invalid value for "Content-Type" header: {}. Expected "application/upload_offset+octet-stream".'
                    .format(content_type), status=status.HTTP_400_BAD_REQUEST)


class TusTerminateMixin(mixins.DestroyModelMixin):
    def destroy(self, request, *args, **kwargs):
        # Retrieve object
        upload = self.get_object()

        # When the upload is still saving, we're not able to destroy the entity
        if upload.state == states.SAVING:
            return Response(_('Unable to terminate upload while in state "{}".'.format(upload.state)),
                            status=status.HTTP_409_CONFLICT)

        # Destroy object
        upload.delete()

        return Response(status=status.HTTP_204_NO_CONTENT)


class UploadViewSet(TusCreateMixin,
                    TusPatchMixin,
                    # mixins.ListModelMixin,
                    # mixins.RetrieveModelMixin,
                    TusHeadMixin,
                    TusTerminateMixin,
                    GenericViewSet):
    serializer_class = UploadSerializer
    metadata_class = UploadMetadata
    lookup_field = 'guid'
    lookup_value_regex = '[a-zA-Z0-9]{8}-[a-zA-Z0-9]{4}-[a-zA-Z0-9]{4}-[a-zA-Z0-9]{4}-[a-zA-Z0-9]{12}'

    def get_queryset(self):
        return get_upload_model().objects.all()