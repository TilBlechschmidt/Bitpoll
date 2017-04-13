import re
from smtplib import SMTPRecipientsRefused

from django.contrib import messages
from django.contrib.auth.models import Group
from django.core.exceptions import ObjectDoesNotExist
from django.core.mail import send_mail
from django.db import transaction, connection, IntegrityError

from django.http import HttpResponseForbidden
from django.shortcuts import redirect, get_object_or_404
from django.db.models import F, Sum, Count, Q
from django.template.loader import render_to_string
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.formats import date_format
from django.utils.translation import ugettext_lazy as _
from django.utils.timezone import activate as tz_activate, localtime, now
from django.utils.timezone import get_current_timezone
from django.views.decorators.http import require_POST

from .forms import PollCreationForm, PollCopyForm, DateChoiceCreationForm, UniversalChoiceCreationForm, \
    DTChoiceCreationDateForm, DTChoiceCreationTimeForm, PollSettingsForm, PollDeleteForm, ChoiceValueForm, CommentForm
from .models import Poll, Choice, ChoiceValue, Vote, VoteChoice, Comment, POLL_RESULTS, PollWatch
from dudel.base.models import DudelUser
from dudel.invitations.models import Invitation

from datetime import datetime, timedelta
from decimal import Decimal
from pytz import all_timezones, timezone
from django.utils.dateparse import parse_datetime, parse_date


def poll(request, poll_url):
    """
    :param request
    :param poll_url: url of poll

    Displays for a given poll its fields along with all possible choices, all votes and all its comments.
    """
    current_poll = get_object_or_404(Poll, url=poll_url)

    tz_activate(current_poll.get_tz_name(request.user))

    poll_votes = Vote.objects.filter(poll=current_poll).order_by(
        'name').select_related()
    # prefetch_related('votechoice_set').select_releated() #TODO (Prefetch objekt nötig, wie ist der reverse join name wirklich?

    matrix = transpose(current_poll.get_choice_group_matrix(get_current_timezone()))

    # aggregate stats for all columns
    stats = Choice.objects.filter(poll=current_poll, deleted=False).order_by('sort_key').annotate(
        score=Sum('votechoice__value__weight')).values('score', 'id', 'text')
    votes_count = poll_votes.count()

    invitations = current_poll.invitation_set.filter(vote=None)
    # The next block is limiting the visibility of the results
    summary = True
    if current_poll.show_results in ("summary", "never"):  # TODO: should the owner see all?
        if request.user.is_authenticated:
            poll_votes = poll_votes.filter(user=request.user)
            invitations = invitations.filter(user=request.user)
        else:
            poll_votes = []
            invitations = []
            messages.info(request, _("No individual results are shown due to Poll settings"))
    elif current_poll.show_results in ("summary after vote", "complete after vote") \
            and (request.user.is_anonymous or request.user not in poll_votes.user_set):
        poll_votes = []
        messages.info(request, _("Results are only sown after (authenticated) Voting"))
        summary = False
    elif current_poll.show_results == "summary after vote":
        poll_votes = poll_votes.filter(user=request.user)
        messages.info(request, _("Only the Summary is shown due to the Poll settings"))
    if current_poll.show_results == "never":
        summary = False

    if not summary:
        messages.info(request, _("The summary is not shown due to the config of the Poll"))

    # aggregate stats for the different Choice_Values per column
    stats2 = Choice.objects.filter(poll=current_poll, deleted=False).order_by('sort_key').annotate(
        count=Count('votechoice__value__color')).values('count', 'id', 'votechoice__value__icon',
                                                        'votechoice__value__color', 'votechoice__value__title')
    #
    # use average for stats
    stats = [
        {
            'score': (stat['score'] / Decimal(votes_count)
                if votes_count > 0 else 0) if stat['score'] is not None else None,
            'count': stat['score'],
            'text': stat,
            'choices': [
                {
                    'count': stat2['count'],
                    'color': stat2['votechoice__value__color'],
                    'icon': stat2['votechoice__value__icon'],
                    'title': stat2['votechoice__value__title']} for
                        stat2 in stats2 if
                        stat2['id'] == stat['id'] and stat2['votechoice__value__color'] != None],
        } for stat in stats]

    poll_watched = False
    if request.user.is_authenticated:
        try:
            poll_watch = PollWatch.objects.get(poll=current_poll, user=request.user)
            poll_watched = bool(poll_watch)
        except ObjectDoesNotExist:
            pass

    max_score = None
    if stats and votes_count > 0:
        max_score_list = [val['score'] for val in stats if val['score'] is not None]
        if max_score_list:
            max_score = max(max_score_list)

    return TemplateResponse(request, "poll/poll.html", {
        'poll': current_poll,
        'matrix': matrix,
        'choices_matrix': zip(matrix, current_poll.choice_set.all()),
        'page': '',
        'votes': poll_votes,
        'stats': stats,
        'max_score': max_score,
        'invitations': invitations if current_poll.show_invitations else [],
        'summary': summary,
        'watched': poll_watched,
        'comment_form': CommentForm(),
        'timezone_warning': (request.user.is_authenticated and
            current_poll.get_tz_name(request.user) != request.user.timezone),
    })


def comment(request, poll_url, comment_id=None):
    """
    Post a comment to a poll
    :param request:
    :param poll_url: the poll url to post to
    :return:
    """
    current_poll = get_object_or_404(Poll, url=poll_url)
    if not current_poll.allow_comments:
        messages.error(_("Comments are disabled for this Poll"))
        return redirect('poll', poll_url)
    user = None
    if not request.user.is_anonymous:
        user = request.user
    if request.POST:
        form = CommentForm(request.POST)
        if form.is_valid():
            text = form.cleaned_data['text']
            name = form.cleaned_data['name']
            if not text:
                messages.error(_("A Comment should have a text"))
            if not user and not name:
                messages.error(_("Provide a name"))
            if comment_id:
                comment = get_object_or_404(Comment, pk=comment_id)
                if comment.can_edit(request.user):
                    comment.text = text
                    comment.name = name
                    comment.save()
                else:
                    messages.error(request, _("You can't edit this Comment"))
            else:
                new_comment = Comment(text=text,
                                      date_created=now(),
                                      name=name,
                                      poll=current_poll,
                                      user=user)
                new_comment.save()
            return redirect('poll', poll_url)
    else:
        if comment_id:
            comment = get_object_or_404(Comment, pk=comment_id)
            if comment.can_edit(request.user):
                form = CommentForm(instance=comment)
            else:
                messages.error(request, _("You can't edit this Comment"))
                return redirect('poll', poll_url)
        else:
            form = CommentForm()
    return TemplateResponse(request, 'poll/comment_edit.html', {
        'comment_form': form,
        'comment_edit_id': comment_id,
        'poll': current_poll
    })


def delete_comment(request, poll_url, comment_id):
    """
    :param request:
    :param poll_url: url of poll belonging to comment
    :param comment_id: ID of comment to be deleted
    :return:

    Case Delete:
        If the user is authenticated and equal to the saved user of the comment, the comment is deleted
        and the user is directed back to the poll's page

        If the user is authenticated but differs from the saved user of the comment, the user is directed
        back with error message "Deletion not allowed. You are not [comment.name]"

        Without authentication the user is directed back with error message "Deletion not allowed.
        You are not authenticated."

    Case Cancel:
        The user is redirected back to the poll's page.
    """
    current_poll = get_object_or_404(Poll, url=poll_url)
    current_comment = get_object_or_404(Comment, id=comment_id)
    error_msg = ""

    if request.method == 'POST':
        if 'Delete' in request.POST:
            if request.user.is_authenticated:
                # TODO additional possibilities of deleting
                if current_comment.can_delete(request.user):
                    current_comment.delete()
                    return redirect('poll', poll_url)
                else:
                    error_msg = _("Deletion not allowed. You are not {}.".format(str(current_comment.name)))
            else:
                error_msg = _("Deletion not allowed. You are not authenticated.")
        else:
            return redirect('poll', poll_url)

    return TemplateResponse(request, 'poll/comment_delete.html', {
        'poll': current_poll,
        'comment': current_comment,
        'error': error_msg,
    })


@require_POST
def watch(request, poll_url):
    current_poll = get_object_or_404(Poll, url=poll_url)
    poll_watch = None

    if not current_poll.can_listen(request.user):
        messages.error(
            request, _("You are not allowed to listen.")
        )
        return redirect('poll', poll_url)

    try:
        poll_watch = PollWatch.objects.get(poll=current_poll, user=request.user)
    except ObjectDoesNotExist:
        pass

    if poll_watch:
        poll_watch = PollWatch.objects.get(poll=current_poll, user=request.user)
        poll_watch.delete()
    else:
        poll_watch = PollWatch(poll=current_poll, user=request.user)
        poll_watch.save()

    return redirect('poll', poll_url)


def edit_choice(request, poll_url):
    current_poll = get_object_or_404(Poll, url=poll_url)
    if not current_poll.can_edit(request.user):
        messages.error(
            request, _("You are not allowed to listen.")
        )
        return redirect('poll', poll_url)

    if current_poll.type == 'universal':
        return redirect('poll_editUniversalChoice', current_poll.url)
    elif current_poll.type == 'date':
        return redirect('poll_editDateChoice', current_poll.url)
    else:
        return redirect('poll_editDTChoiceDate', current_poll.url)
    pass


def edit_date_choice(request, poll_url):
    """
    :param request:
    :param poll_url: url of poll

    Takes several dates as the user's input und checks the validity.
    If the input is valid, for every given date a choice is created and saved. The user is directed to the poll's site.

    If the input is not valid, the user is directed back for correction.
    """
    current_poll = get_object_or_404(Poll, url=poll_url)
    if not current_poll.can_edit(request.user):
        messages.error(
            request, _("You are not allowed to edit this Poll.")
        )
        return redirect('poll', poll_url)

    tz_activate('UTC')
    initial = {
        'dates': ','.join(set(list(
            date_format(localtime(c.date), format='Y-m-d')
            for c in current_poll.choice_set.order_by('sort_key')))),
    }
    if request.method == 'POST':
        form = DateChoiceCreationForm(request.POST, initial=initial)
        if form.is_valid():
            choices = current_poll.choice_set.all()
            # List of the Old Ids, used for detection what has to be deleted
            old_choices_ids = [c.pk for c in choices]
            new_choices = []
            old_choices = []
            dates = []
            error = False
            # clean the data
            for choice in form.cleaned_data['dates'].split(","):
                try:
                    tz = timezone('UTC')
                    date = tz.localize(parse_datetime('{} 0:0'.format(choice)))
                    dates.append(date)
                except ValueError:
                    # This will most likely only happen with users turning of JS
                    error = True
                    messages.error(_("There was en error interpreting the provided dates and times"))
            if not error:
                for i, datum in enumerate(sorted(dates)):
                    choice_objs = Choice.objects.filter(poll=current_poll, date=datum)
                    if choice_objs:
                        choice_obj = choice_objs[0]
                        old_choices_ids.remove(choice_obj.pk)
                        choice_obj.sort_key = i
                        choice_obj.deleted = False
                        old_choices.append(choice_obj)
                    else:
                        new_choices.append(Choice(text="", date=datum, poll=current_poll, sort_key=i))
                with transaction.atomic():
                    Choice.objects.bulk_create(new_choices)
                    for choice in old_choices:
                        choice.save()
                    Choice.objects.filter(pk__in=old_choices_ids).update(deleted=True)
                return redirect('poll', poll_url)
    else:
        form = DateChoiceCreationForm(initial=initial)
    return TemplateResponse(request, "poll/choice_creation_date.html", {
        'poll': current_poll,
        'new_choice': form,
        'page': 'Choices',
        'is_dt_choice': False,
    })


def edit_dt_choice_date(request, poll_url):
    """
    :param request:
    :param poll_url: url of poll

    Takes several dates as the user's input and checks if it's valid.
    If the data is valid, the user is directed to the time-input-site. (The date is passed on as an argument)

    If the data is not valid, the user is directed back for correction.
    """
    current_poll = get_object_or_404(Poll, url=poll_url)
    if not current_poll.can_edit(request.user):
        messages.error(
            request, _("You are not allowed to edit this Poll.")
        )
        return redirect('poll', poll_url)

    tz_activate(current_poll.timezone_name)
    initial = {
        'dates': ','.join(set(list(
            date_format(localtime(c.date), format='Y-m-d')
            for c in current_poll.choice_set.order_by('sort_key')))),
        'times': ','.join(set(list(
            date_format(localtime(c.date), format='H:i')
            for c in current_poll.choice_set.order_by('sort_key')))),
    }
    form = DTChoiceCreationDateForm(initial=initial)
    if request.method == 'POST':
        form = DTChoiceCreationDateForm(
            request.POST, initial=initial)
        if form.is_valid():
            initial['dates'] = form.cleaned_data.get('dates')
            time = DTChoiceCreationTimeForm(initial=initial)
            return TemplateResponse(request, "poll/dt_choice_creation_time.html", {
                'time': time,
                'poll': current_poll,
                'page': 'Choices',
                'step': 2,
            })
    return TemplateResponse(request, "poll/choice_creation_date.html", {
        'new_choice': form,
        'poll': current_poll,
        'step': 1,
        'page': 'Choices',
        'is_dt_choice': True,
    })


def edit_dt_choice_time(request, poll_url):
    """
    :param request:
    :param poll_url: url of poll

    Takes several times as the user's input and checks the validity.
    If the data is valid, the user is directed to the combinations-site, to which all possible combinations of
        dates and times are passed on.
    If the dates are missing, the user is directed back to the date-input-site.
    If the times are missing, the user is directed back to the time-input-site.
    """
    current_poll = get_object_or_404(Poll, url=poll_url)
    if not current_poll.can_edit(request.user):
        messages.error(
            request, _("You are not allowed to edit this Poll.")
        )
        return redirect('poll', poll_url)

    tz_activate(current_poll.timezone_name)
    initial = {
        'dates': ','.join(
            date_format(localtime(c.date), format='Y-m-d')
            for c in current_poll.choice_set.order_by('sort_key')),
        'times': ','.join(set(list(
            date_format(localtime(c.date), format='H:i')
            for c in current_poll.choice_set.order_by('sort_key')))),
    }
    if request.method == 'POST':
        form = DTChoiceCreationTimeForm(request.POST, initial=initial)
        if form.is_valid():
            times = form.cleaned_data['times'].split(',')
            dates = form.cleaned_data['dates'].split(',')

            return TemplateResponse(request, "poll/dt_choice_creation_combinations.html", {
                'times': times,
                'dates': dates,
                'poll': current_poll,
                'page': 'Choices',
                'step': 3,
            })
        elif form.cleaned_data['dates'] != "":
            return TemplateResponse(request, "poll/dt_choice_creation_time.html", {
                'time': form,
                'poll': current_poll,
                'page': 'Choices',
                'step': 2,
            })
    return redirect('poll_editDTChoiceDate', current_poll.url)


def edit_dt_choice_combinations(request, poll_url):
    current_poll = get_object_or_404(Poll, url=poll_url)
    if not current_poll.can_edit(request.user):
        messages.error(
            request, _("You are not allowed to edit this Poll.")
        )
        return redirect('poll', poll_url)

    tz_activate(current_poll.timezone_name)
    if request.method == 'POST':
        # getlist does not raise an exception if datetimes[] is not in request.POST
        chosen_combinations = request.POST.getlist('datetimes[]')

        chosen_times = []
        new_choices = []
        old_choices = []
        choices = current_poll.choice_set.all()
        # List of the Old Ids, used for detection what has to be deleted
        old_choices_ids = [c.pk for c in choices]
        # parse datetime objects of chosen combinations
        for combination in chosen_combinations:
            try:
                tz = timezone(current_poll.timezone_name)
                chosen_times.append(tz.localize(parse_datetime(combination)))
            except ValueError:
                # at least one invalid time/date has been specified. Redirect to first step # TODO: error message spezifizierne
                messages.error(request, _("There was en error interpreting the provided dates and times"))
                return redirect('poll_editDTChoiceDate', current_poll.url)
        # Search for already existing Choices
        for i, date_time in enumerate(sorted(chosen_times)):
            choice_obj = current_poll.choice_set.filter(date=date_time)
            if choice_obj:
                old_choices_ids.remove(choice_obj[0].pk)
                choice_obj[0].sort_key = i
                choice_obj[0].deleted = False  # Mark as not deleted
                old_choices.append(choice_obj[0])
            else:
                new_choices.append(Choice(
                    date=date_time, poll=current_poll, sort_key=i))
        # Save new choices to database, Update/Delete old ones
        with transaction.atomic():
            # Save the new Choices
            Choice.objects.bulk_create(new_choices)
            for choice in old_choices:
                choice.save()
            Choice.objects.filter(pk__in=old_choices_ids).update(deleted=True)
        return redirect('poll', current_poll.url)
    return redirect('poll_editDTChoiceDate', current_poll.url)


def edit_universal_choice(request, poll_url):
    """
    :param request:
    :param poll_url: url of poll

    Takes the text of a choice as the user's input and checks its validity.
    If the input is valid, the choice is saved and the user is directed to the poll's site.

    If the input is not valid, the user is directed back for correction.
    """
    current_poll = get_object_or_404(Poll, url=poll_url)
    if not current_poll.can_edit(request.user):
        messages.error(
            request, _("You are not allowed to edit this Poll.")
        )
        return redirect('poll', poll_url)

    if request.method == 'POST':
        # save new choices
        choice_texts = request.POST.getlist('choice_text')
        choice_sort_keys = request.POST.getlist('choice_sort_key')  # TODO: errorhandling
        for i, choice_text in zip(choice_sort_keys, choice_texts):
            choice_text = choice_text.strip()
            if choice_text == '':
                continue
            choice = Choice(text=choice_text, poll=current_poll, sort_key=i)
            choice.save()

        # update existing choices
        pattern = re.compile(r'^choice_text_(\d+)$')
        with transaction.atomic():
            for choice_id in request.POST.keys():
                choice = pattern.match(choice_id)
                if not choice:
                    continue
                pk = choice.group(1)
                db_choice = get_object_or_404(Choice, poll=current_poll, pk=pk)
                choice_text = request.POST.get(choice_id).strip()
                if choice_text == '':
                    db_choice.deleted = True
                else:
                    db_choice.text = choice_text
                    sort_key = request.POST.get('choice_sort_key_{}'.format(pk), -1)
                    if sort_key == -1 or sort_key == "":
                        sort_key = current_poll.choice_set.count() + 1  # TODO: unter umständen hier max nemen?
                    db_choice.sort_key = sort_key
                    db_choice.save()
        if 'next' in request.POST:
            return redirect('poll', poll_url)
        if 'delete' in request.POST:
            db_choice = get_object_or_404(Choice, poll=current_poll, pk=request.POST.get('delete'))
            db_choice.deleted = not db_choice.deleted
            db_choice.save()

    return TemplateResponse(request, "poll/universal_choice_creation.html", {
        'choices': current_poll.choice_set.all().order_by('sort_key'),
        'poll': current_poll,
        'page': 'Choices',
        'next_sort_key': current_poll.choice_set.count() + 1,  # TODO: unter umständen hier max nemen?
    })


def edit_choicevalues(request, poll_url):
    current_poll = get_object_or_404(Poll, url=poll_url)
    choiceval_select = None
    if not current_poll.can_edit(request.user):
        messages.error(
            request, _("You are not allowed to edit this Poll.")
        )
        return redirect('poll', poll_url)


    if not current_poll.can_listen(request.user):
        messages.error(
            request, _("You are not allowed to listen.")
        )
        return redirect('poll', poll_url)

    form = ChoiceValueForm()
    if request.method == 'POST':
        if not current_poll.can_edit(request.user):
            messages.error(
                request, _("You are not allowed to edit this Poll")
            )
        else:
            if 'delete' in request.POST:
                choiceval_id = request.POST.get('delete', None)
                if choiceval_id:
                    choiceval = get_object_or_404(ChoiceValue, id=choiceval_id)
                    choiceval.delete()
                return redirect('poll_editchoicevalues', current_poll.url)
            elif 'edit' in request.POST:
                choiceval_id = request.POST.get('edit', None)
                if choiceval_id:
                    choiceval_select = get_object_or_404(ChoiceValue, id=choiceval_id)
                    form = ChoiceValueForm(instance=choiceval_select)

    return TemplateResponse(request, 'poll/choicevalue.html', {
        'poll': current_poll,
        'form': form,
        'choiceval_select': choiceval_select,
    })


@require_POST
def edit_choicevalues_create(request, poll_url):
    current_poll = get_object_or_404(Poll, url=poll_url)
    if not current_poll.can_edit(request.user):
        messages.error(
            request, _("You are not allowed to edit this Poll.")
        )
        return redirect('poll', poll_url)

    if not current_poll.can_edit(request.user):
        messages.error(
            request, _("You are not allowed to edit this Poll")
        )
    else:  # TODO exception-handling
        form = ChoiceValueForm(request.POST)
        if form.is_valid():
            title = form.cleaned_data['title']
            color = form.cleaned_data['color']
            icon = form.cleaned_data['icon']
            weight = form.cleaned_data['weight']
            current_id = request.POST.get('choiceval_id', None)

            if current_id:
                current_choiceval = get_object_or_404(ChoiceValue, id=current_id, poll=current_poll)
                current_choiceval.title = title
                current_choiceval.color = color
                current_choiceval.icon = icon
                current_choiceval.weight = weight

                current_choiceval.save()
            else:
                choice_val = ChoiceValue(title=title, icon=icon, color=color, weight=weight, poll=current_poll)
                choice_val.save()
        else:
            choiceval_id = request.POST.get('choiceval_id', None)
            if choiceval_id:
                choiceval_select = get_object_or_404(ChoiceValue, id=choiceval_id)
            else:
                choiceval_select = None
            return TemplateResponse(request, 'poll/choicevalue.html', {
                'poll': current_poll,
                'form': form,
                'choiceval_select': choiceval_select,
            })
    return redirect('poll_editchoicevalues', current_poll.url)


def delete(request, poll_url):
    """
    :param request:
    :param poll_url: url of poll to be deleted
    :return:

    Given Poll is deleted if delete-button is pressed and if user is authenticated.
    Otherwise the user is directed back to the poll's page.
    """
    current_poll = get_object_or_404(Poll, url=poll_url)
    error_msg = ""

    if request.method == 'POST':
        if 'Delete' in request.POST:
            if request.user.is_authenticated:
                # TODO restriction for deletion
                if current_poll.can_edit(request.user):
                    current_poll.delete()
                return redirect('index')
            else:
                error_msg = _("Deletion not allowed. You are not authenticated.")
        else:
            return redirect('poll', poll_url)
    else:
        form = PollDeleteForm(instance=current_poll)

    return TemplateResponse(request, 'poll/poll_delete.html', {
        'poll': current_poll,
        'form': form,
        'error': error_msg,
    })


def vote(request, poll_url, vote_id=None):
    """
    :param request:
    :param poll_url: Url of poll
    :param vote_id: Optional the voteID to edit
    :return:

    Takes vote with comments as input and saves the vote along with all comments.
    """
    current_poll = get_object_or_404(Poll, url=poll_url)

    if current_poll.due_date and current_poll.due_date < now():
        messages.error(
            request, _("This Poll is past the due date, voting is no longer possible")
        )
        return redirect('poll', poll_url)

    tz_activate(current_poll.get_tz_name(request.user))

    if request.method == 'POST':
        if current_poll.can_vote(request.user, request):
            vote_id = request.POST.get('vote_id', None)
            if vote_id:
                current_vote = get_object_or_404(Vote, pk=vote_id, poll=current_poll)
                if not current_vote.can_edit(request.user):
                    # the vote belongs to an user and it is not the authenticated user
                    return HttpResponseForbidden()  # todo: better errorpage?
            else:
                current_vote = Vote(poll=current_poll)
            current_vote.date_created = now()
            current_vote.comment = request.POST.get('comment')
            if vote_id:
                # leave the name as it was
                pass
            elif request.user.is_authenticated:
                current_vote.name = request.user.get_username()
                current_vote.user = request.user

                if request.user.auto_watch:
                    try:
                        poll_watch = PollWatch(poll=current_poll, user=request.user)
                        poll_watch.save()
                    except IntegrityError:
                        pass
            else:
                current_vote.name = request.POST.get('name').strip()
            current_vote.anonymous = 'anonymous' in request.POST

            if not current_poll.anonymous_allowed and current_vote.anonymous:
                messages.error(request, _("Anonymous votes are not allowed for this poll."))
            else:
                if current_vote.anonymous or current_vote.name:
                    # prevent non-anonymous vote without name

                    with transaction.atomic():
                        new_choices = []

                        current_vote.save()

                        if request.user.is_authenticated:
                            # check if this user was invited
                            invitation = current_poll.invitation_set.filter(user=request.user)
                            if invitation:
                                invitation = invitation[0]
                                invitation.vote = current_vote
                                invitation.save()

                        for choice in current_poll.choice_set.all():
                            if str(choice.id) in request.POST:
                                choice_value = get_object_or_404(ChoiceValue, id=request.POST[str(choice.id)])
                            else:
                                choice_value = None
                            new_choices.append(VoteChoice(value=choice_value,
                                                          vote=current_vote,
                                                          choice=choice,
                                                          comment=request.POST.get('comment_{}'.format(choice.id)) or ''))
                        if vote_id:
                            VoteChoice.objects.filter(vote=current_vote).delete()
                            #todo: nochmal prüfen ob das wirjklich das tut was es soll, also erst alles löschen und dann neu anlegen
                            #todo eventuell eine transaktion drum machen? wegen falls das eventuell dazwischen abbricht?
                        else:
                            for current_watch in current_poll.pollwatch_set.all():
                                current_watch.send(request=request, vote=current_vote)

                        VoteChoice.objects.bulk_create(new_choices)
                        messages.success(request, _('Vote has been recorded'))
                        return redirect('poll', poll_url)
                else:
                    messages.error(
                        request, _('You need to either provide a name or post an anonymous vote.'))

    # no/invalid POST: show the dialog
    matrix = current_poll.get_choice_group_matrix(get_current_timezone())
    choices = []
    comments = []
    choice_votes = []
    if vote_id:
        current_vote = get_object_or_404(Vote, pk=vote_id)
    else:
        current_vote = Vote()
    if current_poll.type == 'normal':
        choices_orig = current_poll.choice_set.filter(deleted=False).order_by('sort_key')
    else:
        choices_orig = current_poll.choice_set.filter(deleted=False)
    for choice in choices_orig:
        cur_comment = ""
        value = None
        if request.method == 'POST':
            if str(choice.id) in request.POST:
                value = get_object_or_404(ChoiceValue, id=request.POST[str(choice.id)])
            else:
                value = None
            cur_comment = request.POST.get('comment_{}'.format(choice.id)) or ''
        elif vote_id:  # If we want to edit an vote find the selected fields
            vote_choice = VoteChoice.objects.filter(vote=vote_id, choice=choice.id)
            if vote_choice:  # append the found values
                cur_comment = vote_choice[0].comment
                value = vote_choice[0].value
        choices.append(choice)
        comments.append(cur_comment)
        choice_votes.append(value)
    return TemplateResponse(request, 'poll/vote_creation.html', {
        'poll': current_poll,
        'matrix': matrix,
        'matrix_len': len(matrix[0]),
        'choices_matrix': zip(matrix, choices, comments, choice_votes),
        'choices': current_poll.choice_set.all(),
        'choices_matrix_len': len(choices),
        'values': current_poll.choicevalue_set.all(),
        'page': 'Vote',
        'current_vote': current_vote,
        'timezone_warning': (request.user.is_authenticated and
            current_poll.get_tz_name(request.user) != request.user.timezone),
    })


def vote_assign(request, poll_url, vote_id):
    pass


def vote_delete(request, poll_url, vote_id):
    """
    :param request:
    :param poll_url: url of Poll belonging to vote
    :param vote_id: ID of vote to be deleted
    :return:

    Case Delete:
        If the user is authenticated and is equal to the saved user in the vote, the current vote is deleted.
        The user is redirected to the poll's page.

        If the user is authenticated but not equal to the saved user, the user is directed back with
        error message "Deletion not allowed. You are not [user of vote]".

        If the user is not authenticated, the user is directed back with error message
        "Deletion not allowed. You are not authenticated."

    Case Cancel:
        The user is directed back to the poll's page.
    """
    current_poll = get_object_or_404(Poll, url=poll_url)
    current_vote = get_object_or_404(Vote, id=vote_id)
    error_msg = ""

    if request.method == 'POST':
        if 'Delete' in request.POST:
            if request.user.is_authenticated:
                # TODO additional possibilities of deleting
                if current_vote.can_delete(request.user):
                    current_vote.delete()
                    return redirect('poll', poll_url)
                else:
                    error_msg = _("Deletion not allowed. You are not {}.".format(str(current_vote.name)))
            else:
                error_msg = _("Deletion not allowed. You are not authenticated.")
        else:
            return redirect('poll', poll_url)

    return TemplateResponse(request, 'poll/vote_delete.html', {
        'poll': current_poll,
        'vote': current_vote,
        'error': error_msg,
    })


def copy(request, poll_url):
    """
    :param request:
    :param poll_url: Url of current poll
    :return:

    Takes a new title (optional), a new url (required) and a new due_date (required) as user input.
    Current poll is copied. Title, url and due-date are adapted.
    The new Poll is saved and the user is directed to its page.
    """
    current_poll = get_object_or_404(Poll, url=poll_url)
    date_shift = 0
    error_msg = ""
    if not current_poll.can_listen(request.user):
        messages.error(
            request, _("You are not allowed to listen.")
        )
        return redirect('poll', poll_url)

    if request.method == 'POST' and current_poll.can_listen(request.user):
        form = PollCopyForm(request.POST)

        if form.is_valid():
            copy_choices = form.data.get('copy_choices', '')
            copy_invitations = form.data.get('copy_invitations', '')
            create_invitations_from_votes = form.data.get('create_invitations_from_votes', False)
            copy_answer_values = form.data.get('copy_ans_values', '')
            reset_ownership = form.data.get('reset_ownership', '')
            date_shift = int(form.data.get('date_shift', ''))

            choice_values = current_poll.choicevalue_set.all()
            choices = current_poll.choice_set.all()
            invitations = current_poll.invitation_set.all()
            vote_users = current_poll.vote_set.all().values('user')
            invitation_users = invitations.values('user')

            current_poll.pk = None
            current_poll.title = form.cleaned_data['title']
            current_poll.url = form.cleaned_data['url']
            current_poll.due_date = form.cleaned_data['due_date']

            if date_shift:
                current_poll.due_date += timedelta(days=date_shift)

            if reset_ownership:
                current_poll.user = None
                current_poll.group = None

            current_poll.save()

            if copy_invitations:
                for invitation in invitations:
                    invitation.pk = None
                    invitation.poll = current_poll
                    invitation.date_created = now()
                    invitation.last_email = None
                    invitation.creator = request.user
                    invitation.save()
                    invitation.send(request)

            if create_invitations_from_votes:
                for user in vote_users:
                    if user not in invitation_users:
                        invitation = Invitation(poll=current_poll, user=user, date_created=now(),
                                                creator=request.user)
                        invitation.save()
                        invitation.send(request)

            if copy_choices:
                for choice in choices:
                    choice.pk = None
                    choice.poll = current_poll
                    if date_shift and choice.date:
                        choice.date += timedelta(days=date_shift)
                    choice.save()

            if copy_answer_values:
                for value in choice_values:
                    value.pk = None
                    value.poll = current_poll
                    value.save()

            return redirect('poll', current_poll.url)

    else:
        form = PollCopyForm({'title': "Copy of " + current_poll.title, 'due_date': current_poll.due_date})

    return TemplateResponse(request, 'poll/copy.html', {
        'form': form,
        'poll': current_poll,
        'date_shift': date_shift,
        'error': error_msg,
    })


def settings(request, poll_url):
    """

    :param request:
    :param poll_url:
    :return:
    """
    current_poll = get_object_or_404(Poll, url=poll_url)
    groups = None

    if request.user.is_authenticated:
        groups = Group.objects.filter(user=request.user)

    if not current_poll.can_edit(request.user):
        messages.error(
            request, _("You are not allowed to edit this Poll.")
        )
        return redirect('poll', poll_url)

    user_error = ""
    error_msg = ""
    user = current_poll.user.username if current_poll.user else ""
    if request.method == 'POST':
        form = PollSettingsForm(request.POST, instance=current_poll)
        if not current_poll.can_edit(request.user):
            error_msg = _("You are not allowed to edit this Poll")
        elif form.is_valid():
            new_poll = form.save(commit=False)
            user = form.data.get('user', '')
            if user:
                try:
                    user_obj = DudelUser.objects.get(username=user)
                    new_poll.user = user_obj
                except ObjectDoesNotExist:  # TODO: correct exception
                    user_error = _("User {} not Found".format(user))
            if not user_error:
                new_poll.save()
                messages.success(request, _('Settings have been changed'))
                return redirect('poll_settings', current_poll.url)
        else:
            user = form.cleaned_data.get('user', '')
            print(form.errors)

    else:
        form = PollSettingsForm(instance=current_poll)

    return TemplateResponse(request, 'poll/settings.html', {
        'perm_error': error_msg,
        'form': form,
        'poll': current_poll,
        'page': 'Settings',
        'groups': groups,
        'results': POLL_RESULTS,
        'timezones': all_timezones,
        'user_error': user_error,
        'user': user,
    })


def transpose(matrix):
    return [list(i) for i in zip(*matrix)]
