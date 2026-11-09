import pytest
from directory_seats.client import DirectoryClient, DirectoryApiError
from catchall_mail.sources import ImapSource
from test_mail_sources import FakeIMAP, SETTINGS


def test_directory_rejects_repeated_pagination_tokens():
    class Response:
        status_code = 200
        def json(self):
            return {'users': [], 'nextPageToken': 'repeated'}
    class Session:
        calls = 0
        def get(self, *args, **kwargs):
            self.calls += 1
            if self.calls > 3:
                pytest.fail('pagination loop not bounded')
            return Response()
    session = Session()
    with pytest.raises(DirectoryApiError):
        DirectoryClient(session=session).list_users('example.test')
    assert session.calls == 2


def test_special_inbox_case_uses_same_checkpoint_namespace():
    env = {'MAIL_USER': 'user', 'MAIL_PASSWORD': 'secret'}
    ids = []
    for mailbox in ('INBOX', 'inbox', 'Inbox'):
        source = ImapSource(dict(SETTINGS, mailbox=mailbox), environ=env, transport_factory=FakeIMAP)
        ids.append(source.pull_with_metadata(1)[0].account_id)
    assert len(set(ids)) == 1
