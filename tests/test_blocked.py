from core.runner import blocked_question


def test_report_about_the_check_is_not_a_wait():
    text = ('Done.\n\n- ⚠️ It came back as "Needs me" by mistake: my old summary said "re-queue the QR '
            'ticket when you\'re ready", and the runner read that as waiting on you')
    assert blocked_question(text) == ""


def test_quoted_phrase_is_not_a_wait():
    assert blocked_question('Fixed the check so "waiting on you" in a quote is ignored.') == ""


def test_real_wait_still_caught():
    assert blocked_question("Built the call script. Waiting on you to pick the SIM number.").startswith("Blocked")
    assert blocked_question("Everything is ready; I can't continue without your Telegram code.").startswith("Blocked")


def test_dev_merge_is_never_a_wait():
    assert blocked_question("Once it's merged into dev the feature is live.") == ""
