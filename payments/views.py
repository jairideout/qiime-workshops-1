# ----------------------------------------------------------------------------
# Copyright (c) 2016--, QIIME development team.
#
# Distributed under the terms of the Modified BSD License.
#
# The full license is in the file LICENSE, distributed with this software.
# ----------------------------------------------------------------------------

import logging
from decimal import Decimal
from urllib.parse import quote_plus as qp

from django.http import HttpResponse, HttpResponseRedirect
from django.core.urlresolvers import reverse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import ListView, DetailView, TemplateView, View
from django.views.generic.edit import FormMixin
from django.conf import settings
from django.contrib import messages

import requests
from extra_views import FormSetView
from markdownx.utils import markdownify

from .models import Workshop, Order, OrderItem, Rate
from .forms import OrderForm, OrderDetailForm, OrderDetailFormSet

logger = logging.getLogger(__name__)


class SessionConfirmMixin(object):
    def get(self, request, *args, **kwargs):
        if 'order' not in request.session:
            url = reverse('payments:details',
                          kwargs={'slug': kwargs['slug']})
            return HttpResponseRedirect(url)
        return super().get(request, *args, **kwargs)


class WorkshopList(ListView):
    context_object_name = 'upcoming_workshops'
    private_code = None

    # Since there are possible GET parameters we need to conditionally
    # determine the queryset for the ListView
    def get_queryset(self):
        queryset = None
        if self.private_code:
            queryset = Workshop.objects.filter(private_code=self.private_code)

        if queryset is None or len(queryset) == 0:
            queryset = Workshop.objects.filter(draft=False)
        return queryset

    # Capture the GET and store it in the user session. This allows
    # for blocking the ability to see a workshop detail page even if you know
    # the slug for it.
    def get(self, request, *args, **kwargs):
        self.private_code = request.GET.get('code')
        if self.private_code:
            request.session['private_code'] = self.private_code
        return super().get(request, *args, **kwargs)


class WorkshopDetail(FormMixin, DetailView):
    model = Workshop
    form_class = OrderForm
    context_object_name = 'workshop'

    def get(self, request, *args, **kwargs):
        self.request.session['discount_code'] = self.request.GET.get('rate')
        response = super().get(request, *args, **kwargs)
        if not request.user.is_authenticated() and self.object.draft:
            return HttpResponseRedirect(reverse('payments:index'))
        return response

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['workshop'] = self.object
        kwargs['discount_code'] = self.request.session.get('discount_code')
        return kwargs

    def get_context_data(self, **kwargs):
        code = self.request.session.get('private_code')
        discount_code = self.request.session.get('discount_code')
        context = super().get_context_data(**kwargs)
        context['form'] = self.get_form()

        rates = []
        for rate in self.object.filter_rates(discount_code):
            field = context['form'][rate.name]
            rates.append({'field': field, 'name': rate.name,
                          'price': rate.price, 'sold_out': rate.sold_out})
        context['rates'] = rates
        context['workshop'].description = \
            markdownify(context['workshop'].description)

        purchasable = (self.object.public or (not self.object.public and
                       code == self.object.private_code))
        context['purchasable'] = purchasable

        return context

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        form = self.get_form()
        if form.is_valid():
            return self.form_valid(form)
        else:
            return self.form_invalid(form)

    def form_valid(self, form):
        order = form.data.copy()
        order['workshop'] = self.object.slug
        order['rates'] = list(form.rate_set.values('id', 'name'))
        order_total = 0
        for rate in form.rate_set:
            order_total += Decimal(form.data[rate.name]) * rate.price

        order['order_total'] = str(order_total)
        self.request.session['order'] = order
        return super().form_valid(form)

    def get_success_url(self):
        return reverse('payments:order_details',
                       kwargs={'slug': self.object.slug})


class OrderDetail(SessionConfirmMixin, FormSetView):
    template_name = 'payments/order_detail.html'
    form_class = OrderDetailForm
    formset_class = OrderDetailFormSet
    extra = 0

    def dispatch(self, request, *args, **kwargs):
        self.slug = kwargs['slug']
        return super().dispatch(request, *args, **kwargs)

    def get_initial(self):
        order = self.request.session['order']
        initial = []
        for rate in order['rates']:
            for ticket in range(int(order[rate['name']])):
                data = {'rate': rate['id']}
                initial.append(data)
        self.initial = initial
        return initial

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        zipped = []
        for i, form in enumerate(context['formset']):
            rate = Rate.objects.get(pk=self.initial[i]['rate'])
            data = {'form': form, 'rate': rate}
            zipped.append(data)
        context['zipped'] = zipped
        context['workshop'] = Workshop.objects.get(slug=self.slug)
        return context

    def formset_valid(self, formset):
        suborder = formset.data.copy()
        order = self.request.session['order']
        tickets = []
        total_forms = suborder['form-TOTAL_FORMS']
        for i in range(int(total_forms)):
            rate = suborder['form-%s-rate' % i]
            email = suborder['form-%s-email' % i]
            name = suborder['form-%s-name' % i]
            tickets.append({'rate': rate, 'email': email, 'name': name})
        order['tickets'] = tickets
        self.request.session['order'] = order
        return super().formset_valid(formset)

    def get_success_url(self):
        return reverse('payments:confirm',
                       kwargs={'slug': self.slug})


class ConfirmOrder(SessionConfirmMixin, TemplateView):
    template_name = 'payments/workshop_confirm.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        order = self.request.session['order']
        tickets = []
        for ticket in order['tickets']:
            tickets.append({'name': ticket['name'], 'email': ticket['email'],
                            'rate': Rate.objects.get(pk=ticket['rate'])})
        context['tickets'] = tickets
        context['order_name'] = order['name']
        context['order_email'] = order['email']
        context['order_total'] = order['order_total']
        context['workshop'] = Workshop.objects.get(slug=kwargs['slug'])
        return context


class SubmitOrder(View):
    http_method_names = ['post']

    def post(self, request, *args, **kwargs):
        order_data = request.session['order']

        rates = {}
        for ticket in order_data['tickets']:
            rate = Rate.objects.get(id=ticket['rate'])
            if rate not in rates:
                rates[rate] = 1
            else:
                rates[rate] += 1

        for rate, quantity in rates.items():
            if rate.ticket_count + quantity > rate.capacity:
                remaining = rate.capacity - rate.ticket_count
                messages.warning(request, 'There are too many %s tickets in '
                                 'this order. Ticket(s) remaining at this '
                                 'rate: %d.' % (rate.name, remaining))
                return HttpResponseRedirect(
                    reverse('payments:details', kwargs={
                        'slug': order_data['workshop']
                    })
                )

        order = Order.objects.create(contact_email=order_data['email'],
                                     contact_name=order_data['name'],
                                     order_total=order_data['order_total'])
        items = []
        for ticket in order_data['tickets']:
            items.append(OrderItem(order=order, rate_id=ticket['rate'],
                         email=ticket['email'], name=ticket['name']))
        # Hit the database only once and create all of the OrderItems generated
        OrderItem.objects.bulk_create(items)

        # Now that the order is saved, clear the session so that they can't
        # resubmit the order
        del request.session['order']

        name = order.contact_name.split(' ')

        payload = {
            'LMID':                    settings.LMID,
            'unique_id':               str(order.transaction_id),
            'sTotal':                  str(order.order_total),
            'webTitle':                settings.PAYMENT_TITLE,
            'Trans_Desc':              settings.PAYMENT_DESCRIPTION,
            'contact_info':            settings.PAYMENT_CONTACT_INFO,
            'BILL_CUSTOMER_EMAIL':     order.contact_email,
            'BILL_CUSTOMER_FIRSTNAME': qp(name[0]),
            'BILL_CUSTOMER_LASTNAME':  qp(name[-1]) if len(name) > 1 else '',
            'note':                    '',  # Placeholder
            'arrayname':               'metadata',
        }

        for i, ticket in enumerate(order_data['tickets']):
            rate = Rate.objects.get(pk=ticket['rate'])
            payload['metadata_item_%s,%s' % (0, i)] = \
                '%s: %s (%s)' % (rate.name, qp(ticket['name']),
                                 ticket['email'])
            payload['metadata_item_%s,%s' % (1, i)] = '1'
            payload['metadata_item_%s,%s' % (2, i)] = str(rate.price)
            payload['metadata_item_%s,%s' % (3, i)] = str(rate.price)
            payload['metadata_item_%s,%s' % (4, i)] = settings.PSF_SPEEDTYPE
            payload['metadata_item_%s,%s' % (5, i)] = settings.PSF_ACCT_NUMBER

        r = requests.post(settings.PAYMENT_URL, data=payload,
                          verify=settings.PAYMENT_CERT_BUNDLE)
        return HttpResponse(r.content, status=r.status_code)


@method_decorator(csrf_exempt, name='dispatch')
class OrderCallback(View):
    http_method_names = ['post']

    def post(self, request, *args, **kwargs):
        try:
            order = Order.objects.get(transaction_id=request.POST['unique_id'])
            order.billed_total = request.POST['amount']
            order.billed_datetime = request.POST['date_time']
            order.save()
        except (Order.DoesNotExist, KeyError) as e:
            logger.error('%s: %s' % (e, request.body))
            return HttpResponse(status=400)
        return HttpResponse()
