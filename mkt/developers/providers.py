import os
import uuid
from datetime import datetime

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ObjectDoesNotExist

import bleach
import commonware
from curling.lib import HttpClientError
from tower import ugettext_lazy as _

from amo.urlresolvers import reverse
from constants.payments import (PROVIDER_BANGO, PROVIDER_BOKU,
                                PROVIDER_REFERENCE)
from lib.crypto import generate_key
# Because client is used in the classes, renaming here for clarity.
from lib.pay_server import client as pay_client
from mkt.constants.payments import ACCESS_PURCHASE
from mkt.developers import forms_payments
from mkt.developers.models import PaymentAccount, SolitudeSeller
from mkt.developers.utils import uri_to_pk
from mkt.webpay.webpay_jwt import WebAppProduct

root = 'developers/payments/includes/'

log = commonware.log.getLogger('z.devhub.providers')


def account_check(f):
    """
    Use this decorator on Provider methods to ensure that the account
    being passed into the method belongs to that provider.
    """
    def wrapper(self, *args, **kwargs):
        for arg in args:
            if (isinstance(arg, PaymentAccount)
                and arg.provider != self.provider):
                raise ValueError('Wrong account {0} != {1}'
                                 .format(arg.provider, self.provider))
        return f(self, *args, **kwargs)
    return wrapper


class Provider(object):
    generic = pay_client.api.generic

    def account_create(self, user, form_data):
        raise NotImplementedError

    @account_check
    def account_retrieve(self, account):
        raise NotImplementedError

    @account_check
    def account_update(self, account, form_data):
        raise NotImplementedError

    @account_check
    def generic_create(self, account, app, secret):
        # This sets the product up in solitude.
        external_id = WebAppProduct(app).external_id()
        data = {'seller': uri_to_pk(account.seller_uri),
                'external_id': external_id}

        # Create the generic product.
        try:
            generic = self.generic.product.get_object_or_404(**data)
        except ObjectDoesNotExist:
            generic = self.generic.product.post(data={
                'seller': account.seller_uri, 'secret': secret,
                'external_id': external_id, 'public_id': str(uuid.uuid4()),
                'access': ACCESS_PURCHASE,
            })

        return generic

    @account_check
    def product_create(self, account, app, secret):
        raise NotImplementedError

    def setup_seller(self, user):
        log.info('[User:{0}] Creating seller'.format(user.pk))
        return SolitudeSeller.create(user)

    def setup_account(self, **kw):
        log.info('[User:{0}] Created payment account (uri: {1})'
                 .format(kw['user'].pk, kw['uri']))
        kw.update({'seller_uri': kw['solitude_seller'].resource_uri,
                   'provider': self.provider})
        return PaymentAccount.objects.create(**kw)

    @account_check
    def terms_create(self, account):
        raise NotImplementedError

    @account_check
    def terms_retrieve(self, account):
        raise NotImplementedError

    def get_portal_url(self, app_slug):
        return ''


class Bango(Provider):
    """
    The special Bango implementation.
    """
    bank_values = (
        'seller_bango', 'bankAccountPayeeName', 'bankAccountNumber',
        'bankAccountCode', 'bankName', 'bankAddress1', 'bankAddress2',
        'bankAddressZipCode', 'bankAddressIso'
    )
    client = pay_client.api.bango
    # This is at the new provider API.
    client_provider = pay_client.api.provider.bango
    forms = {
        'account': forms_payments.BangoPaymentAccountForm,
    }
    full = 'Bango'
    name = 'bango'
    package_values = (
        'adminEmailAddress', 'supportEmailAddress', 'financeEmailAddress',
        'paypalEmailAddress', 'vendorName', 'companyName', 'address1',
        'address2', 'addressCity', 'addressState', 'addressZipCode',
        'addressPhone', 'countryIso', 'currencyIso', 'vatNumber'
    )
    provider = PROVIDER_BANGO
    templates = {
        'add': os.path.join(root, 'add_payment_account_bango.html'),
        'edit': os.path.join(root, 'edit_payment_account_bango.html'),
    }

    def account_create(self, user, form_data):
        # Get the seller object.
        user_seller = self.setup_seller(user)

        # Get the data together for the package creation.
        package_values = dict((k, v) for k, v in form_data.items() if
                              k in self.package_values)
        # Dummy value since we don't really use this.
        package_values.setdefault('paypalEmailAddress', 'nobody@example.com')
        package_values['seller'] = user_seller.resource_uri

        log.info('[User:%s] Creating Bango package' % user)
        res = self.client.package.post(data=package_values)
        uri = res['resource_uri']

        # Get the data together for the bank details creation.
        bank_details_values = dict((k, v) for k, v in form_data.items() if
                                   k in self.bank_values)
        bank_details_values['seller_bango'] = uri

        log.info('[User:%s] Creating Bango bank details' % user)
        self.client.bank.post(data=bank_details_values)
        return self.setup_account(user=user,
                                  uri=res['resource_uri'],
                                  solitude_seller=user_seller,
                                  account_id=res['package_id'],
                                  name=form_data['account_name'])

    @account_check
    def account_retrieve(self, account):
        data = {'account_name': account.name}
        package_data = (self.client.package(uri_to_pk(account.uri))
                        .get(data={'full': True}))
        data.update((k, v) for k, v in package_data.get('full').items() if
                    k in self.package_values)
        return data

    @account_check
    def account_update(self, account, form_data):
        account.update(name=form_data.pop('account_name'))
        self.client.api.by_url(account.uri).patch(
            data=dict((k, v) for k, v in form_data.items() if
                      k in self.package_values))

    @account_check
    def product_create(self, account, app):
        secret = generate_key(48)
        generic = self.generic_create(account, app, secret)
        product_uri = generic['resource_uri']
        data = {'seller_product': uri_to_pk(product_uri)}

        # There are specific models in solitude for Bango details.
        # These are SellerBango and SellerProductBango that store Bango
        # details such as the Bango Number.
        #
        # Solitude calls Bango to set up whatever it needs.
        try:
            res = self.client.product.get_object_or_404(**data)
        except ObjectDoesNotExist:
            # The product does not exist in Solitude so create it.
            res = self.client_provider.product.post(data={
                'seller_bango': account.uri,
                'seller_product': product_uri,
                'name': unicode(app.name),
                'packageId': account.account_id,
                'categoryId': 1,
                'secret': secret
            })

        return res['resource_uri']

    @account_check
    def terms_update(self, account):
        package = self.client.package(account.uri).get_object_or_404()
        account.update(agreed_tos=True)
        return self.client.sbi.post(data={
            'seller_bango': package['resource_uri']})

    @account_check
    def terms_retrieve(self, account):
        package = self.client.package(account.uri).get_object_or_404()
        res = self.client.sbi.agreement.get_object(data={
            'seller_bango': package['resource_uri']})
        if 'text' in res:
            res['text'] = bleach.clean(res['text'], tags=['h3', 'h4', 'br',
                                                          'p', 'hr'])
        return res

    def get_portal_url(self, app_slug):
        url = 'mkt.developers.apps.payments.bango_portal_from_addon'
        return reverse(url, args=[app_slug]) if app_slug else ''


class Reference(Provider):
    """
    The reference implementation provider. If another provider
    implements to the reference specification, then it should be able to
    just inherit from this with minor changes.
    """
    client = pay_client.api.provider.reference
    forms = {
        'account': forms_payments.ReferenceAccountForm,
    }
    full = _('Reference Implementation')
    name = 'reference'
    provider = PROVIDER_REFERENCE
    templates = {
        'add': os.path.join(root, 'add_payment_account_reference.html'),
        'edit': os.path.join(root, 'edit_payment_account_reference.html'),
    }

    def account_create(self, user, form_data):
        user_seller = self.setup_seller(user)
        form_data.update({'uuid': user_seller.uuid, 'status': 'ACTIVE'})
        name = form_data.pop('account_name')
        res = self.client.sellers.post(data=form_data)
        return self.setup_account(user=user,
                                  uri=res['resource_uri'],
                                  solitude_seller=user_seller,
                                  account_id=res['id'],
                                  name=name)

    @account_check
    def account_retrieve(self, account):
        data = {'account_name': account.name}
        data.update(self.client.sellers(account.account_id).get())
        return data

    @account_check
    def account_update(self, account, form_data):
        account.update(name=form_data.pop('account_name'))
        self.client.sellers(account.account_id).put(form_data)

    @account_check
    def product_create(self, account, app):
        secret = generate_key(48)
        generic = self.generic_create(account, app, secret)

        # These just pass straight through to zippy to create the product
        # and don't create any intermediate objects in solitude.
        #
        # Until bug 948240 is fixed, we have to do this, again.
        try:
            created = self.client.products.get(
                external_id=generic['external_id'],
                seller_id=uri_to_pk(account.uri))
        except HttpClientError:
            created = []

        if len(created) > 1:
            raise ValueError('Zippy returned more than one resource.')

        elif len(created) == 1:
            return created[0]['resource_uri']

        created = self.client.products.post(data={
            'external_id': generic['external_id'],
            'seller_id': uri_to_pk(account.uri),
            'name': unicode(app.name),
            'uuid': str(uuid.uuid4()),
        })
        return created['resource_uri']

    @account_check
    def terms_retrieve(self, account):
        res = self.client.terms(account.account_id).get()
        if 'text' in res:
            res['text'] = bleach.clean(res['text'])
        return res

    @account_check
    def terms_update(self, account):
        account.update(agreed_tos=True)
        # GETed data from Zippy needs to be reformated prior to be PUT
        # until bug 966096 is fixed.
        data = self.client.sellers(account.account_id).get()
        for field in ['id', 'resource_uri', 'resource_name']:
            del data[field]
        data['agreement'] = datetime.now().strftime('%Y-%m-%d')
        return self.client.sellers(account.account_id).put(data)


class Boku(Provider):
    """
    Specific Boku implementation details.
    """
    client = pay_client.api.boku
    forms = {
        'account': forms_payments.BokuAccountForm,
    }
    full = _('Boku')
    name = 'boku'
    provider = PROVIDER_BOKU
    templates = {
        'add': os.path.join(root, 'add_payment_account_boku.html'),
        'edit': os.path.join(root, 'edit_payment_account_boku.html'),
    }

    def account_create(self, user, form_data):
        user_seller = self.setup_seller(user)
        form_data.update({'seller': user_seller.resource_uri})
        name = form_data.pop('account_name')
        res = self.client.seller.post(data=form_data)
        return self.setup_account(account_id=res['id'],
                                  agreed_tos=True,
                                  name=name,
                                  solitude_seller=user_seller,
                                  user=user,
                                  uri=res['resource_uri'])

    @account_check
    def account_retrieve(self, account):
        return {}

    def terms_retrieve(self, account):
        return {'accepted': True}

    def terms_update(self, account):
        account.update(agreed_tos=True)
        return {'accepted': True}

    @account_check
    def product_create(self, account, app):
        secret = generate_key(48)
        created = self.generic_create(account, app, secret)
        # Note this is setting the generic product account,
        # not the specific product uri.
        return created['resource_uri']

    def terms_retrieve(self, account):
        return {'agreed': True}

    def terms_update(self, account):
        account.update(agreed_tos=True)
        return {'agreed': True}

    def get_portal_url(self, app_slug):
        return settings.BOKU_PORTAL


ALL_PROVIDERS = ALL_PROVIDERS_BY_ID = {}
for p in (Bango, Reference, Boku):
    ALL_PROVIDERS[p.name] = p
    ALL_PROVIDERS_BY_ID[p.provider] = p


def get_provider(name=None, id=None):
    """
    This returns the default provider so we can provide backwards capability
    for API's that expect get_provider to return 'bango'. This is something
    we'll have to clean out.
    """
    if id is not None:
        provider = ALL_PROVIDERS_BY_ID[id]()
    else:
        if name is None:
            name = settings.DEFAULT_PAYMENT_PROVIDER
        provider = ALL_PROVIDERS[name]()
    if provider.name not in settings.PAYMENT_PROVIDERS:
        raise ImproperlyConfigured(
            'The provider {p} is not one of the '
            'allowed PAYMENT_PROVIDERS.'.format(p=provider.name))
    return provider


def get_providers():
    return [ALL_PROVIDERS[name]() for name in settings.PAYMENT_PROVIDERS]
