# -*- coding: utf-8 -*-
# Copyright (C) 1998-2016 by the Free Software Foundation, Inc.
#
# This file is part of Postorius.
#
# Postorius is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version.
#
# Postorius is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for
# more details.
#
# You should have received a copy of the GNU General Public License along with
# Postorius.  If not, see <http://www.gnu.org/licenses/>.


import logging
import datetime

from django.forms import formset_factory
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from django.http import Http404
from django.core.urlresolvers import reverse
from django.conf import settings

from postorius.models import UnsubscriberStats

try:
    from urllib2 import HTTPError
except ImportError:
    from urllib.error import HTTPError

from postorius import utils
from postorius.models import (MailmanConnectionError, MailmanApiError, List,
                              AddressConfirmationProfile, MailmanUser,
                              Mailman404Error)
from postorius.forms import (UserPreferences, AddressActivationForm,
                             ChangeSubscriptionForm, ChangeDisplayNameForm)
from postorius.views.generic import MailmanUserView
from smtplib import SMTPException
from socket import error as socket_error
import errno
import uuid


logger = logging.getLogger(__name__)


class UserMailmanSettingsView(MailmanUserView):
    """The logged-in user's global Mailman Preferences."""

    @method_decorator(login_required)
    def post(self, request):
        try:
            mm_user = MailmanUser.objects.get(address=request.user.email)
            form = UserPreferences(request.POST)
            if form.is_valid():
                preferences = mm_user.preferences
                for key in form.fields.keys():
                    if form.cleaned_data[key] is not None:
                        # None: nothing set yet. Remember to remove this test
                        # when Mailman accepts None as a "reset to default"
                        # value.
                        preferences[key] = form.cleaned_data[key]
                preferences.save()
                messages.success(request,
                                 _('Your preferences have been updated.'))
            else:
                messages.error(request, _('Something went wrong.'))
        except MailmanApiError:
            return utils.render_api_error(request)
        except Mailman404Error as e:
            messages.error(request, e.msg)
        return redirect("user_mailmansettings")

    @method_decorator(login_required)
    def get(self, request):
        try:
            mm_user = MailmanUser.objects.get_or_create_from_django(
                request.user)
        except MailmanApiError:
            return utils.render_api_error(request)
        settingsform = UserPreferences(initial=mm_user.preferences)
        return render(request, 'postorius/user/mailman_settings.html',
                      {'mm_user': mm_user, 'settingsform': settingsform})


class UserAddressPreferencesView(MailmanUserView):
    """The logged-in user's address-based Mailman Preferences."""

    @method_decorator(login_required)
    def post(self, request):
        try:
            mm_user = MailmanUser.objects.get(address=request.user.email)
            formset_class = formset_factory(UserPreferences)
            formset = formset_class(request.POST)
            if formset.is_valid():
                for form, address in zip(formset.forms, mm_user.addresses):
                    preferences = address.preferences
                    for key in form.fields.keys():
                        if form.cleaned_data[key] is not None:
                            # None: nothing set yet. Remember to remove this
                            # test when Mailman accepts None as a
                            # "reset to default" value.
                            preferences[key] = form.cleaned_data[key]
                    preferences.save()
                messages.success(request,
                                 _('Your preferences have been updated.'))
            else:
                messages.error(request, _('Something went wrong.'))
        except MailmanApiError:
            return utils.render_api_error(request)
        except HTTPError as e:
            messages.error(request, e.msg)
        return redirect("user_address_preferences")

    @method_decorator(login_required)
    def get(self, request):
        try:
            helperform = UserPreferences()
            mm_user = MailmanUser.objects.get(address=request.user.email)
            addresses = mm_user.addresses
            AFormset = formset_factory(UserPreferences, extra=len(addresses))
            formset = AFormset()
            zipped_data = zip(formset.forms, addresses)
            for form, address in zipped_data:
                form.initial = address.preferences
        except MailmanApiError:
            return utils.render_api_error(request)
        except Mailman404Error:
            return render(request, 'postorius/user/address_preferences.html',
                          {'nolists': 'true'})
        return render(request, 'postorius/user/address_preferences.html',
                      {'mm_user': mm_user, 'addresses': addresses,
                       'helperform': helperform, 'formset': formset,
                       'zipped_data': zipped_data})


@login_required
def user_list_options(request, list_id):
    utils.set_other_emails(request.user)
    mlist = List.objects.get_or_404(fqdn_listname=list_id)
    mm_user = MailmanUser.objects.get(address=request.user.email)
    subscription = None
    for s in mm_user.subscriptions:
        if s.role == 'member' and s.list_id == list_id:
            subscription = s
            break
    if not subscription:
        raise Http404(_('Subscription does not exist'))
    preferences = subscription.preferences
    if request.method == 'POST':
        form = UserPreferences(request.POST)
        if form.is_valid():
            for key in form.cleaned_data.keys():
                if form.cleaned_data[key] is not None:
                    # None: nothing set yet. Remember to remove this test
                    # when Mailman accepts None as a "reset to default"
                    # value.
                    preferences[key] = form.cleaned_data[key]
            preferences.save()
            messages.success(request, _('Your preferences have been updated.'))
            return redirect('user_list_options', list_id)
        else:
            messages.error(request, _('Something went wrong.'))
    else:
        form = UserPreferences(initial=subscription.preferences)
    user_emails = [request.user.email] + request.user.other_emails
    subscription_form = ChangeSubscriptionForm(
        user_emails, initial={'email': subscription.email})
    return render(request, 'postorius/user/list_options.html',
                  {'form': form, 'list': mlist,
                   'change_subscription_form': subscription_form})


class UserSubscriptionPreferencesView(MailmanUserView):
    """The logged-in user's subscription-based Mailman Preferences."""

    @method_decorator(login_required)
    def post(self, request):
        try:
            subscriptions = self._get_memberships()
            formset_class = formset_factory(UserPreferences)
            formset = formset_class(request.POST)
            if formset.is_valid():
                for form, subscription in zip(formset.forms, subscriptions):
                    preferences = subscription.preferences
                    for key in form.cleaned_data.keys():
                        if form.cleaned_data[key] is not None:
                            # None: nothing set yet. Remember to remove this
                            # test when Mailman accepts None as a
                            # "reset to default" value.
                            preferences[key] = form.cleaned_data[key]
                    preferences.save()
                    
                    date = datetime.datetime.now()
                    if form.cleaned_data['delivery_status'] == 'by_user':
                        stats = UnsubscriberStats.create(subscription.list_id,request.user.email,"Disabled",date,request.user.id,request.user)

                        stats.save()
                messages.success(request,
                                 _('Your preferences have been updated.'))
            else:
                messages.error(request, _('Something went wrong.'))
        except MailmanApiError:
            return utils.render_api_error(request)
        except HTTPError as e:
            messages.error(request, e.msg)
        return redirect("user_subscription_preferences")

    @method_decorator(login_required)
    def get(self, request):
        try:
            subscriptions = self._get_memberships()
            Mformset = formset_factory(
                UserPreferences, extra=len(subscriptions))
            formset = Mformset()
            zipped_data = zip(formset.forms, subscriptions)
            for form, subscription in zipped_data:
                form.initial = subscription.preferences
        except MailmanApiError:
            return utils.render_api_error(request)
        except Mailman404Error:
            return render(request,
                          'postorius/user/subscription_preferences.html',
                          {'nolists': 'true'})
        return render(request, 'postorius/user/subscription_preferences.html',
                      {'zipped_data': zipped_data, 'formset': formset})


@login_required
def user_subscriptions(request):
    """Shows the subscriptions of a user."""
    utils.set_other_emails(request.user)
    try:
        mm_user = MailmanUser.objects.get_or_create_from_django(request.user)
    except MailmanApiError:
        return utils.render_api_error(request)
    memberships = [m for m in mm_user.subscriptions if m.role == 'member']
    return render(request, 'postorius/user/subscriptions.html',
                  {'memberships': memberships})


@login_required()
def user_profile(request):
    utils.set_other_emails(request.user)
    try:
        mm_user = MailmanUser.objects.get_or_create_from_django(request.user)
    except MailmanApiError:
        return utils.render_api_error(request)
    if request.method == 'POST':
        if request.POST.get('formname') == 'displayname':
            display_name_form = ChangeDisplayNameForm(request.POST)
            form = AddressActivationForm(
                initial={'user_email': request.user.email})
            if display_name_form.is_valid():
                name = display_name_form.cleaned_data['display_name']
                try:
                    mm_user.display_name = name
                    mm_user.save()
                except MailmanApiError:
                    return utils.render_api_error(request)
                except HTTPError as e:
                    messages.error(request, e)
                else:
                    messages.success(request, _('Display name changed'))
                return redirect('user_profile')
        else:
            display_name_form = ChangeDisplayNameForm(
                initial={'display_name': mm_user.display_name})
            form = AddressActivationForm(request.POST)
            if form.is_valid():
                profile, c = (
                    AddressConfirmationProfile.objects.update_or_create(
                        email=form.cleaned_data['email'], user=request.user,
                        defaults={'activation_key': uuid.uuid4().hex}))
                try:
                    profile.send_confirmation_link(request)
                    messages.success(request, _(
                                     'Please follow the instructions sent via'
                                     ' email to confirm the address'))
                    return redirect('user_profile')
                except (SMTPException, socket_error) as e:
                    if (not isinstance(e, SMTPException) and
                            e.errno != errno.ECONNREFUSED):
                        raise e
                    profile.delete()
                    messages.error(request,
                                   _('Currently emails can not be added,'
                                     ' please try again later'))
    else:
        form = AddressActivationForm(
            initial={'user_email': request.user.email})
        display_name_form = ChangeDisplayNameForm(
            initial={'display_name': mm_user.display_name})
    return render(request, 'postorius/user/profile.html',
                  {'mm_user': mm_user, 'form': form,
                   'name_form': display_name_form})


@login_required()
def address_activation_link(request, activation_key):
    """
    Checks the given activation_key. If it is valid, the saved address will be
    added to mailman. Also, the corresponding profile record will be removed.
    If the key is not valid, it will be ignored.
    """
    try:
        profile = AddressConfirmationProfile.objects.get(
            activation_key=activation_key)
        if request.user != profile.user:
            return redirect('{}?next={}'.format(
                reverse(settings.LOGIN_URL), request.path))
        if not profile.is_expired:
            # Add the address to the user record in Mailman.
            logger.info('Adding address %s to %s', profile.email,
                        request.user.email)
            try:
                try:
                    mailman_user = MailmanUser.objects.get(
                        address=request.user.email)
                except Mailman404Error:
                    mailman_user = MailmanUser.objects.create(
                        request.user.email, '')
                # If the adress already exists, it's an import artefact: it's been
                # validated by email, so it's safe to merge.
                mm_address = mailman_user.add_address(
                    profile.email, absorb_existing=True)
                # The address has just been verified.
                if not mm_address.verified_on:
                    mm_address.verify()
            except (MailmanApiError, MailmanConnectionError):
                messages.error(request, _('The address could not be added.'))
                return
            # Reset the other_emails cache
            if hasattr(request.user, 'other_emails'):
                del request.user.other_emails
            utils.set_other_emails(request.user)
            messages.success(request,
                             _('The email address has been activated!'))
        else:
            messages.error(request, _('The activation link has expired,'
                                      ' please add the email again!'))
        profile.delete()
    except AddressConfirmationProfile.DoesNotExist:
        messages.error(request, _('The activation link is invalid'))
    return redirect('user_profile')
