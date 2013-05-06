"""
This is an experiment in splitting up the monolithic model view class into
smaller, more reusable parts, and using class-based views at the same time
(not the generic class based views, though)
"""

import json

from django import forms
from django.contrib import messages
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.forms.models import modelform_factory, model_to_dict
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import ugettext as _
from django.views.generic.base import TemplateView

from towel.forms import BatchForm, towel_formfield_callback
from towel.paginator import Paginator, EmptyPage, InvalidPage
from towel.utils import changed_regions, safe_queryset_and, tryreverse


class ModelResourceView(TemplateView):
    base_template = 'base.html'
    model = None
    queryset = None
    template_name_suffix = None

    def get_context_data(self, **kwargs):
        opts = self.model._meta
        context = {
            'base_template': self.base_template,
            'verbose_name': opts.verbose_name,
            'verbose_name_plural': opts.verbose_name_plural,
            'view': self,

            'add_url': tryreverse(
                '%(app_label)s_%(module_name)s_add' % opts.__dict__),
            'list_url': tryreverse(
                '%(app_label)s_%(module_name)s_list' % opts.__dict__),
            }
        context.update(kwargs)
        return context

    def get_template_names(self):
        opts = self.model._meta
        names = [
            '{}/{}{}.html'.format(opts.app_label, opts.module_name,
                self.template_name_suffix),
            'resources/object{}.html'.format(self.template_name_suffix),
            ]
        if self.template_name:
            names.insert(0, self.template_name)
        return names

    def get_queryset(self):
        if self.queryset is not None:
            return self.queryset._clone()
        elif self.model is not None:
            return self.model._default_manager.all()
        else:
            raise ImproperlyConfigured("'%s' must define 'queryset' or 'model'"
                                       % self.__class__.__name__)

    def get_object(self):
        return get_object_or_404(self.get_queryset(), **self.kwargs)

    def allow_add(self, silent=True):
        return True
    def allow_edit(self, object=None, silent=True):
        return True
    def allow_delete(self, object=None, silent=True):
        if not silent:
            opts = self.model._meta
            if object is None:
                messages.error(self.request, _('You are not allowed to'
                    ' delete %(verbose_name_plural)s.') % opts.__dict__)
            else:
                messages.error(self.request, _('You are not allowed to'
                    ' delete this %(verbose_name)s.') % opts.__dict__)
        return False


class ListView(ModelResourceView):
    paginate_by = None
    search_form = None
    template_name_suffix = '_list'

    def get_paginate_by(self, queryset):
        """
        if self.paginate_all_allowed and self.request.GET.get('all'):
            return None
        """
        return self.paginate_by

    def get_context_data(self, object_list, **kwargs):
        context = super(ListView, self).get_context_data(
            object_list=object_list, **kwargs)

        paginate_by = self.get_paginate_by(object_list)
        if paginate_by:
            paginator = Paginator(object_list, paginate_by)

            try:
                page = int(self.request.GET.get('page'))
            except (TypeError, ValueError):
                page = 1
            try:
                page = paginator.page(page)
            except (EmptyPage, InvalidPage):
                page = paginator.page(paginator.num_pages)

            context.update({
                'object_list': page.object_list,
                'page': page,
                'paginator': paginator,
                })

        return context

    def get(self, request, *args, **kwargs):
        self.object_list = self.get_queryset()

        context = {}

        self.object_list, response = self.handle_search_form(
            context, self.object_list)
        if response:
            return response

        response = self.handle_batch_form(context, self.object_list)
        if response:
            return response

        context.update(self.get_context_data(object_list=self.object_list))
        return self.render_to_response(context)

    def handle_search_form(self, context, queryset=None):
        """
        Must return a tuple consisting of a queryset and either a HttpResponse
        or ``None``
        """

        if queryset is None:
            queryset = self.get_query_set(self.request)

        if self.search_form:
            form = self.search_form(self.request.GET, request=self.request)
            if not form.is_valid():
                messages.error(self.request,
                    _('The search query was invalid.'))

                if self.request.get_full_path().endswith('?clear=1'):
                    # No redirect loop generation
                    raise ImproperlyConfigured(
                        'Search form %r does not validate after'
                        ' clearing.' % form)

                return queryset, HttpResponseRedirect('?clear=1')

            queryset = safe_queryset_and(queryset, form.queryset(self.model))

            context['search_form'] = form

        return queryset, None

    def get_batch_actions(self):
        """
        Returns a list of batch action tuples ``(key, name, handler_fn)``

        * ``key``: Something nice, such as ``delete_selected``.
        * ``name``: Will be shown in the dropdown.
        * ``handler_fn``: Callable. Receives the request and the queryset.
        """
        return [
            ('delete_selected', _('Delete selected'), self.delete_selected),
            ]

    def handle_batch_form(self, ctx, queryset):
        """
        May optionally return a HttpResponse which is directly returned to the
        browser
        """

        actions = self.get_batch_actions()
        if not actions:
            return

        class _Form(BatchForm):
            def __init__(self, *args, **kwargs):
                super(_Form, self).__init__(*args, **kwargs)
                self.actions = actions
                self.fields['action'] = forms.ChoiceField(
                    label=_('Action'),
                    choices=[('', '---------')] + [row[:2] for row in actions],
                    widget=forms.HiddenInput,
                    )

            def process(self):
                action = self.cleaned_data.get('action')
                for row in actions:
                    if action == row[0]:
                        return row[2](self.request, self.batch_queryset)

        form = _Form(self.request, queryset)
        ctx['batch_form'] = form

        if form.should_process():
            result = form.process()

            if isinstance(result, HttpResponse):
                return result

            elif hasattr(result, '__iter__'):
                messages.success(self.request,
                    _('Processed the following items: <br>\n %s') % (
                        u'<br>\n '.join(
                            unicode(item) for item in result)))

            elif result is not None:
                # Not None, but cannot make sense of it either.
                raise TypeError(u'Return value %r of %s.process() invalid.' % (
                    result,
                    form.__class__.__name__,
                    ))

            info = (
                self.model._meta.app_label,
                self.model._meta.module_name,
                )
            url = tryreverse('%s_%s_list' % info)
            return HttpResponseRedirect(url if url else '.')

    def batch_action_hidden_fields(self, queryset, additional=[]):
        """
        Returns a blob of HTML suitable for jumping back into the batch
        action handler. Most useful for batch action handlers needing to
        present a confirmation and/or form page to the user.

        See ``delete_selected`` below for the usage.
        """
        post_values = [('batchform', 1)] + additional + [
            ('batch_%s' % item.pk, '1') for item in queryset]

        return u'\n'.join(
            u'<input type="hidden" name="%s" value="%s">' % item
            for item in post_values)

    def delete_selected(self, request, queryset):
        # HACK ALARM. XXX ugly. Makes ModelView.add_message never add a
        # message again in this request/response cycle.
        class Bla(list):
            def __contains__(self, key):
                return True
        request._towel_add_message_ignore = Bla()

        allowed = [self.allow_delete(item) for item in queryset]
        queryset = [item for item, perm in zip(queryset, allowed) if perm]

        if not queryset:
            messages.error(request, _('You are not allowed to delete any'
                ' object in the selection.'))
            return

        elif not all(allowed):
            messages.warning(request,
                _('Deletion of some objects not allowed. Those have been'
                    ' excluded from the selection already.'))

        if 'confirm' in request.POST:
            messages.success(request, _('Deletion successful.'))
            # Call all delete() methods individually
            [item.delete() for item in queryset]
            return

        context = super(ListView, self).get_context_data(
            title=_('Delete selected'),
            action_queryset=queryset,
            action_hidden_fields=self.batch_action_hidden_fields(
                queryset, [
                    ('batch-action', 'delete_selected'),
                    ('confirm', 1),
                    ]),
            )
        self.template_name_suffix = '_action'
        return self.render_to_response(context)


class DetailView(ModelResourceView):
    template_name_suffix = '_detail'

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        context = self.get_context_data(object=self.object)
        return self.render_to_response(context)


class FormView(ModelResourceView):
    form_class = forms.ModelForm
    object = None
    template_name_suffix = '_form'

    def get_form_kwargs(self, **kwargs):
        kw = {'instance': self.object}
        if self.request.method in ('POST', 'PUT'):
            kw.update({
                'data': self.request.POST,
                'files': self.request.FILES,
                })
        kw.update(kwargs)
        return kw

    def get_form_class(self):
        return modelform_factory(self.model,
            form=self.form_class,
            formfield_callback=towel_formfield_callback,
            )

    def get_form(self):
        return self.get_form_class()(**self.get_form_kwargs())

    def form_valid(self, form):
        self.object = form.save()
        messages.success(self.request,
            _('The %(verbose_name)s has been successfully saved.') %
                self.object._meta.__dict__)
        return redirect(self.object)

    def form_invalid(self, form):
        context = self.get_context_data(form=form, object=self.object)
        return self.render_to_response(context)


class AddView(FormView):
    def get(self, request, *args, **kwargs):
        if not self.allow_add(silent=False):
            return redirect('../')  # TODO fix target
        form = self.get_form()
        return self.render_to_response(self.get_context_data(form=form))

    def post(self, request, *args, **kwargs):
        if not self.allow_add(silent=False):
            return redirect('../')  # TODO fix target
        form = self.get_form()
        if form.is_valid():
            return self.form_valid(form)
        return self.form_invalid(form)


class EditView(FormView):
    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        if not self.allow_edit(self.object, silent=False):
            return redirect(self.object)
        form = self.get_form()
        context = self.get_context_data(form=form, object=self.object)
        return self.render_to_response(context)

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        if not self.allow_edit(self.object, silent=False):
            return redirect(self.object)
        form = self.get_form()
        if form.is_valid():
            return self.form_valid(form)
        return self.form_invalid(self, form)


class LiveFormView(FormView):
    form_class = forms.ModelForm

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        if not self.allow_edit(self.object, silent=True):
            raise PermissionDenied

        form_class = self.get_form_class()
        data = model_to_dict(self.object,
            fields=form_class._meta.fields,
            exclude=form_class._meta.exclude,
            )
        for key, value in request.POST.items():
            data[key] = value

        form = form_class(**self.get_form_kwargs(data=data))

        if form.is_valid():
            self.object = form.save()

            # TODO make this pluggable
            regions = {}
            context = self.get_context_data(
                object=self.object,
                regions=regions,
                )
            self.render_to_response(context)
            return HttpResponse(
                json.dumps(changed_regions(regions, form.changed_data)),
                content_type='application/json')

        return HttpResponse(unicode(form.errors))


class DeleteView(ModelResourceView):
    template_name_suffix = '_delete_confirmation'
    form_class = forms.Form

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()

        if not self.allow_delete(self.object, silent=False):
            return redirect(self.object)

        if request.method == 'POST':
            form = self.form_class(request.POST)

            if form.is_valid():
                # TODO messages.success()
                self.object.delete()
                return self.object.urls.url('list')
        else:
            form = self.form_class()

        context = self.get_context_data(
            object=self.object,
            form=form,
            )
        return self.render_to_response(context)


class MultitenancyMixin(object):
    def get_queryset(self):
        if self.queryset is not None:
            return safe_queryset_and(self.queryset,
                self.queryset.model._default_manager.for_access(
                    self.request.access))
        elif self.model is not None:
            return self.model._default_manager.for_access(
                self.request.access)
        else:
            raise ImproperlyConfigured("'%s' must define 'queryset' or 'model'"
                                       % self.__class__.__name__)

    def get_form_kwargs(self, **kwargs):
        kwargs['request'] = self.request
        return super(MultitenancyMixin, self).get_form_kwargs(**kwargs)