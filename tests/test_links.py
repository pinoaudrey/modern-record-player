from vinyl.links import parse_ref


def test_track_share_link():
    ref = parse_ref("https://open.spotify.com/track/4hTErxf8ZqFNGH0hZqEoAI?si=b8af6025f06a4ee4")
    assert ref.type == "track"
    assert ref.id == "4hTErxf8ZqFNGH0hZqEoAI"
    assert ref.uri == "spotify:track:4hTErxf8ZqFNGH0hZqEoAI"


def test_album_link_with_intl_prefix():
    ref = parse_ref("https://open.spotify.com/intl-de/album/0JGOiO34nwfUdDrD612dOp")
    assert ref.type == "album"
    assert ref.id == "0JGOiO34nwfUdDrD612dOp"


def test_playlist_link():
    ref = parse_ref("https://open.spotify.com/playlist/66r7S3FuK7h9a930TOQSYH?si=x&pt=y")
    assert ref.type == "playlist"


def test_artist_link():
    ref = parse_ref("open.spotify.com/artist/1RyvyyTE3xzB2ZywiAwp0i")
    assert ref.type == "artist"


def test_bare_uri():
    ref = parse_ref("spotify:album:22py1IeIi51c0GBYEHQTsI")
    assert ref.type == "album"
    assert ref.id == "22py1IeIi51c0GBYEHQTsI"


def test_uri_embedded_in_text():
    ref = parse_ref("check this out spotify:playlist:1FIFVq4IwEPDm6sqXItXVc thanks")
    assert ref.type == "playlist"


def test_garbage_returns_none():
    assert parse_ref("https://example.com/track/nope") is None
    assert parse_ref("") is None
    assert parse_ref("spotify:show:12345") is None


def test_short_id_rejected():
    assert parse_ref("https://open.spotify.com/track/abc123") is None
