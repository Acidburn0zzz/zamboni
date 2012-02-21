from django import forms
from django.conf import settings
from django.utils.safestring import mark_safe

import happyforms
from tower import ugettext as _, ugettext_lazy as _lazy

import amo
from files.models import FileUpload
from users.models import UserProfile
from webapps.models import Webapp


class DevAgreementForm(happyforms.ModelForm):
    read_dev_agreement = forms.BooleanField(
        label=mark_safe(_lazy('<b>Agree</b> and Continue')),
        widget=forms.HiddenInput)

    class Meta:
        model = UserProfile
        fields = ('read_dev_agreement',)


def verify_app_domain(manifest_url):
    if settings.WEBAPPS_UNIQUE_BY_DOMAIN:
        domain = Webapp.domain_from_url(manifest_url)
        if Webapp.objects.filter(app_domain=domain).exists():
            raise forms.ValidationError(
                _('An app already exists on this domain; '
                  'only one app per domain is allowed.'))


class NewWebappForm(happyforms.Form):
    upload = forms.ModelChoiceField(widget=forms.HiddenInput,
        queryset=FileUpload.objects.filter(valid=True),
        error_messages={'invalid_choice': _lazy('There was an error with your '
                                                'upload. Please try again.')})

    def clean_upload(self):
        upload = self.cleaned_data['upload']
        verify_app_domain(upload.name)  # JS puts manifest URL here.
        return upload


class PremiumTypeForm(happyforms.Form):
    premium_type = forms.TypedChoiceField(coerce=lambda x: int(x),
                                choices=amo.ADDON_PREMIUM_TYPES.items(),
                                widget=forms.RadioSelect())
