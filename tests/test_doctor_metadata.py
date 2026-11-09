import json
from gsuite_seatpool.cli import main


def test_doctor_reports_local_default_not_google_entitlement(capsys):
    assert main(['doctor']) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['default_seat_count'] == 50
    assert 'cloud_identity_free_cap' not in output
