from datetime import datetime, timedelta
from django_webtest import WebTest
from django.test.utils import override_settings
from django.test import TestCase
from django import VERSION
from django.core.urlresolvers import reverse
try:
    from django.contrib.auth import get_user_model
    User = get_user_model()
except ImportError:  # django 1.4 compatibility
    from django.contrib.auth.models import User
from django.contrib.admin.util import quote
from django.conf import settings
from simple_history.models import HistoricalRecords
from simple_history.templatetags import simple_history_compare

from ..models import Book, Person, Poll, State


today = datetime(2021, 1, 1, 10, 0)
tomorrow = today + timedelta(days=1)


def get_history_url(obj, history_index=None, site="admin"):
    try:
        app, model = obj._meta.app_label, obj._meta.module_name
    except AttributeError:
        app, model = obj._meta.app_label, obj._meta.model_name
    if history_index is not None:
        history = obj.history.order_by('history_id')[history_index]
        return reverse(
            "{site}:{app}_{model}_simple_history".format(
                site=site, app=app, model=model),
            args=[quote(obj.pk), quote(history.history_id)],
        )
    else:
        return reverse("{site}:{app}_{model}_history".format(
            site=site, app=app, model=model), args=[quote(obj.pk)])


class AdminTest(WebTest):

    def setUp(self):
        self.user = User.objects.create_superuser('user_login',
                                                  'u@example.com', 'pass')

    def tearDown(self):
        try:
            del HistoricalRecords.thread.request
        except AttributeError:
            pass

    def login(self, user=None):
        if user is None:
            user = self.user
        form = self.app.get(reverse('admin:index')).maybe_follow().form
        form['username'] = user.username
        form['password'] = 'pass'
        return form.submit()


class AdminSiteTest(AdminTest):

    def test_history_list(self):
        if VERSION >= (1, 5):
            try:
                module_name = self.user._meta.module_name
            except AttributeError:
                module_name = self.user._meta.model_name
            self.assertEqual(module_name, 'customuser')
        self.login()
        poll = Poll(question="why?", pub_date=today)
        poll._history_user = self.user
        poll.save()
        response = self.app.get(get_history_url(poll))
        self.assertIn(get_history_url(poll, 0), response.unicode_normal_body)
        self.assertIn("Poll object", response.unicode_normal_body)
        self.assertIn("Created", response.unicode_normal_body)
        self.assertIn(self.user.username, response.unicode_normal_body)

    def test_history_form_permission(self):
        self.login(self.user)
        person = Person.objects.create(name='Sandra Hale')
        self.app.get(get_history_url(person, 0), status=403)

    def test_invalid_history_form(self):
        self.login()
        poll = Poll.objects.create(question="why?", pub_date=today)
        response = self.app.get(get_history_url(poll, 0))
        response.form['question'] = ""
        response = response.form.submit()
        self.assertEqual(response.status_code, 200)
        self.assertIn("This field is required", response.unicode_normal_body)

    def test_history_form(self):
        self.login()
        poll = Poll.objects.create(question="why?", pub_date=today)
        poll.question = "how?"
        poll.save()

        # Make sure form for initial version is correct
        response = self.app.get(get_history_url(poll, 0))
        self.assertEqual(response.form['question'].value, "why?")
        self.assertEqual(response.form['pub_date_0'].value, "2021-01-01")
        self.assertEqual(response.form['pub_date_1'].value, "10:00:00")

        # Create new version based on original version
        response.form['question'] = "what?"
        response.form['pub_date_0'] = "2021-01-02"
        response = response.form.submit()
        self.assertEqual(response.status_code, 302)
        if VERSION < (1, 4, 0):
            self.assertTrue(response.headers['location']
                            .endswith(get_history_url(poll)))
        else:
            self.assertTrue(response.headers['location']
                            .endswith(reverse('admin:tests_poll_changelist')))

        # Ensure form for second version is correct
        response = self.app.get(get_history_url(poll, 1))
        self.assertEqual(response.form['question'].value, "how?")
        self.assertEqual(response.form['pub_date_0'].value, "2021-01-01")
        self.assertEqual(response.form['pub_date_1'].value, "10:00:00")

        # Ensure form for new third version is correct
        response = self.app.get(get_history_url(poll, 2))
        self.assertEqual(response.form['question'].value, "what?")
        self.assertEqual(response.form['pub_date_0'].value, "2021-01-02")
        self.assertEqual(response.form['pub_date_1'].value, "10:00:00")

        # Ensure current version of poll is correct
        poll = Poll.objects.get()
        self.assertEqual(poll.question, "what?")
        self.assertEqual(poll.pub_date, tomorrow)
        self.assertEqual([p.history_user for p in Poll.history.all()],
                         [self.user, None, None])

    def test_history_user_on_save_in_admin(self):
        self.login()

        # Ensure polls created via admin interface save correct user
        add_page = self.app.get(reverse('admin:tests_poll_add'))
        add_page.form['question'] = "new poll?"
        add_page.form['pub_date_0'] = "2012-01-01"
        add_page.form['pub_date_1'] = "10:00:00"
        changelist_page = add_page.form.submit().follow()
        self.assertEqual(Poll.history.get().history_user, self.user)

        # Ensure polls saved on edit page in admin interface save correct user
        change_page = changelist_page.click("Poll object")
        change_page.form.submit()
        self.assertEqual([p.history_user for p in Poll.history.all()],
                         [self.user, self.user])

    def test_underscore_in_pk(self):
        self.login()
        book = Book(isbn="9780147_513731")
        book._history_user = self.user
        book.save()
        response = self.app.get(get_history_url(book))
        self.assertIn(book.history.all()[0].revert_url(),
                      response.unicode_normal_body)

    def test_historical_user_no_setter(self):
        """Demonstrate admin error without `_historical_user` setter.
        (Issue #43)

        """
        self.login()
        add_page = self.app.get(reverse('admin:tests_document_add'))
        self.assertRaises(AttributeError, add_page.form.submit)

    def test_historical_user_with_setter(self):
        """Documented work-around for #43"""
        self.login()
        add_page = self.app.get(reverse('admin:tests_paper_add'))
        add_page.form.submit()

    def test_history_user_not_saved(self):
        self.login()
        poll = Poll.objects.create(question="why?", pub_date=today)
        historical_poll = poll.history.all()[0]
        self.assertIsNone(
            historical_poll.history_user,
            "No way to know of request, history_user should be unset.",
        )

    def test_middleware_saves_user(self):
        overridden_settings = {
            'MIDDLEWARE_CLASSES':
                settings.MIDDLEWARE_CLASSES
                + ['simple_history.middleware.HistoryRequestMiddleware'],
        }
        with override_settings(**overridden_settings):
            self.login()
            poll = Poll.objects.create(question="why?", pub_date=today)
            historical_poll = poll.history.all()[0]
            self.assertEqual(historical_poll.history_user, self.user,
                             "Middleware should make the request available to "
                             "retrieve history_user.")
                            
    def test_middleware_anonymous_user(self):
        overridden_settings = {
            'MIDDLEWARE_CLASSES':
                settings.MIDDLEWARE_CLASSES
                + ['simple_history.middleware.HistoryRequestMiddleware'],
        }
        with override_settings(**overridden_settings):
            self.app.get(reverse('admin:index'))
            poll = Poll.objects.create(question="why?", pub_date=today)
            historical_poll = poll.history.all()[0]
            self.assertEqual(historical_poll.history_user, None,
                             "Middleware request user should be able to "
                             "be anonymous.")

    def test_other_admin(self):
        """Test non-default admin instances.

        Make sure non-default admin instances can resolve urls and
        render pages.
        """
        self.login()
        state = State.objects.create()
        history_url = get_history_url(state, site="other_admin")
        self.app.get(history_url)
        change_url = get_history_url(state, 0, site="other_admin")
        self.app.get(change_url)

    def test_compare_history(self):
        self.login()


class CompareHistoryTest(AdminTest):

    def setUp(self):
        super(CompareHistoryTest, self).setUp()
        self.login()
        self.poll = Poll.objects.create(question="Who?", pub_date=today)
        for question in ("What?", "Where?", "When?", "Why?", "How?"):
            self.poll.question = question
            self.poll.save()

    def test_navigate_to_compare(self):
        response = self.app.get(get_history_url(self.poll)).form.submit()
        response.mustcontain("<title>Compare ")


class CompareTableTest(TestCase):

    def test_diff_table(self):
        table_markup = simple_history_compare.diff_table(a="this\nan\ntest", b="this\nis\na\ntest")
        self.assertEqual(table_markup, """
    <table class="diff" id="difflib_chg_to1__top"
           cellspacing="0" cellpadding="0" rules="groups" >
        <colgroup></colgroup> <colgroup></colgroup> <colgroup></colgroup>
        <colgroup></colgroup> <colgroup></colgroup> <colgroup></colgroup>
        
        <tbody>
            <tr><td class="diff_next" id="difflib_chg_to1__0"><a href="#difflib_chg_to1__0">f</a></td><td class="diff_header" id="from1_1">1</td><td nowrap="nowrap">this</td><td class="diff_next"><a href="#difflib_chg_to1__0">f</a></td><td class="diff_header" id="to1_1">1</td><td nowrap="nowrap">this</td></tr>
            <tr><td class="diff_next"><a href="#difflib_chg_to1__top">t</a></td><td class="diff_header" id="from1_2">2</td><td nowrap="nowrap"><span class="diff_sub">an</span></td><td class="diff_next"><a href="#difflib_chg_to1__top">t</a></td><td class="diff_header" id="to1_2">2</td><td nowrap="nowrap"><span class="diff_add">is</span></td></tr>
            <tr><td class="diff_next"></td><td class="diff_header"></td><td nowrap="nowrap"></td><td class="diff_next"></td><td class="diff_header" id="to1_3">3</td><td nowrap="nowrap"><span class="diff_add">a</span></td></tr>
            <tr><td class="diff_next"></td><td class="diff_header" id="from1_3">3</td><td nowrap="nowrap">test</td><td class="diff_next"></td><td class="diff_header" id="to1_4">4</td><td nowrap="nowrap">test</td></tr>
        </tbody>
    </table>""")
