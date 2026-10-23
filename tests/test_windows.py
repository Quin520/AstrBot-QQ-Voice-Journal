from voice_journal.audio_windows import review_windows


def test_long_turn_samples_beginning_middle_and_end():
    windows = review_windows([{"start": 10, "end": 70}])
    assert len(windows) == 3
    assert windows[0][0] == 10
    assert windows[-1][1] == 70
    assert all(end - start <= 8 for start, end in windows)
    assert windows[0][1] <= windows[1][0] < windows[1][1] <= windows[2][0]


def test_short_turns_are_not_duplicated_to_meet_training_duration():
    assert review_windows([{"start": 1, "end": 4}]) == [(1, 4)]
