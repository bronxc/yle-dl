# -*- coding: utf-8 -*-

from __future__ import print_function, absolute_import, unicode_literals
import attr
import copy
import json
import os
import pytest
from yledl import StreamFilters, IOContext, BackendFactory, \
    RD_SUCCESS, RD_FAILED
from yledl.backends import BaseDownloader
from yledl.downloader import YleDlDownloader
from yledl.extractors import Clip, FailedClip, StreamFlavor, Subtitle
from utils import Capturing


class StateCollectingBackend(BaseDownloader):
    def __init__(self, state_dict, id):
        BaseDownloader.__init__(self, '.mp4')
        self.id = id
        self.state_dict = state_dict

    def save_stream(self, clip_title, io):
        self.state_dict['command'] = 'download'
        self.state_dict['stream_id'] = self.id

        return RD_SUCCESS

    def pipe(self, io, subtitle_url):
        self.state_dict['command'] = 'pipe'
        self.state_dict['stream_id'] = self.id

        return RD_SUCCESS

    def next_available_filename(self, proposed):
        return proposed

    def warn_on_unsupported_feature(self, io):
        pass


class MockStream(object):
    def __init__(self, state_dict, id):
        self.id = id
        self.state_dict = state_dict

    def is_valid(self):
        return True

    def get_error_message(self):
        return None

    def to_url(self):
        return 'https://example.com/video/{}.mp4'.format(self.id)

    def create_downloader(self, backends):
        return StateCollectingBackend(self.state_dict, self.id)


def successful_clip(state_dict, title='Test clip: S01E01-2018-07-01T00:00'):
    return Clip(
        webpage='https://areena.yle.fi/1-1234567',
        flavors=[
            # The flavors are intentionally unsorted
            StreamFlavor(
                media_type='video',
                height=1080,
                width=1920,
                bitrate=2808,
                streams=[MockStream(state_dict, 'high_quality')]
            ),
            StreamFlavor(
                media_type='video',
                height=360,
                width=640,
                bitrate=880,
                streams=[MockStream(state_dict, 'low_quality')]
            ),
            StreamFlavor(
                media_type='video',
                height=720,
                width=1280,
                bitrate=1412,
                streams=[MockStream(state_dict, 'medium_quality')]
            ),
            StreamFlavor(
                media_type='video',
                height=720,
                width=1280,
                bitrate=1872,
                streams=[MockStream(state_dict, 'medium_quality_high_bitrate')]
            )
        ],
        title=title,
        duration_seconds=950,
        region='Finland',
        publish_timestamp='2018-07-01T00:00:00+03:00',
        expiration_timestamp='2019-01-01T00:00:00+03:00',
        subtitles=[
            Subtitle('https://example.com/subtitle/fin.srt', 'fin'),
            Subtitle('https://example.com/subtitle/swe.srt', 'swe')
        ]
    )


def failed_clip():
    return FailedClip('https://areena.yle.fi/1-1234567', 'Failed test clip')


@attr.s
class DownloaderParametersFixture(object):
    io = attr.ib()
    filters = attr.ib()
    downloader = attr.ib()


@pytest.fixture
def simple():
    return DownloaderParametersFixture(
        io=IOContext(destdir='/tmp/'),
        filters=StreamFilters(),
        downloader=YleDlDownloader([BackendFactory.ADOBEHDSPHP])
    )


def test_download_success(simple):
    state = {}
    clips = [successful_clip(state)]
    res = simple.downloader.download_episodes(
        clips, simple.io, simple.filters, None)

    assert res == RD_SUCCESS
    assert state['command'] == 'download'
    assert state['stream_id'] == 'high_quality'


def test_download_filter_resolution(simple):
    state = {}
    filters = StreamFilters(maxheight=700)
    clips = [successful_clip(state)]
    res = simple.downloader.download_episodes(clips, simple.io, filters, None)

    assert res == RD_SUCCESS
    assert state['command'] == 'download'
    assert state['stream_id'] == 'low_quality'


def test_download_filter_exact_resolution(simple):
    state = {}
    filters = StreamFilters(maxheight=720)
    clips = [successful_clip(state)]
    res = simple.downloader.download_episodes(clips, simple.io, filters, None)

    assert res == RD_SUCCESS
    assert state['command'] == 'download'
    assert state['stream_id'] == 'medium_quality'


def test_download_filter_bitrate1(simple):
    state = {}
    filters = StreamFilters(maxbitrate=1500)
    clips = [successful_clip(state)]
    res = simple.downloader.download_episodes(clips, simple.io, filters, None)

    assert res == RD_SUCCESS
    assert state['command'] == 'download'
    assert state['stream_id'] == 'medium_quality'


def test_download_filter_bitrate2(simple):
    state = {}
    filters = StreamFilters(maxbitrate=2000)
    clips = [successful_clip(state)]
    res = simple.downloader.download_episodes(clips, simple.io, filters, None)

    assert res == RD_SUCCESS
    assert state['command'] == 'download'
    assert state['stream_id'] == 'medium_quality_high_bitrate'


def test_download_multiple_filters(simple):
    state = {}
    filters = StreamFilters(maxheight=720, maxbitrate=1000)
    clips = [successful_clip(state)]
    res = simple.downloader.download_episodes(clips, simple.io, filters, None)

    assert res == RD_SUCCESS
    assert state['command'] == 'download'
    assert state['stream_id'] == 'low_quality'


def test_pipe_success(simple):
    state = {}
    clips = [successful_clip(state)]
    res = simple.downloader.pipe(clips, simple.io, simple.filters)

    assert res == RD_SUCCESS
    assert state['command'] == 'pipe'
    assert state['stream_id'] == 'high_quality'


def test_print_urls(simple):
    state = {}
    clips = [successful_clip(state)]

    res = None
    with Capturing() as output:
        res = simple.downloader.print_urls(clips, simple.filters)

    assert res == RD_SUCCESS
    assert output == ['https://example.com/video/high_quality.mp4']
    assert 'command' not in state


def test_print_titles(simple):
    titles = ['Uutiset', 'Pasila: S01E01-2018-07-01T00:00']
    clips = [successful_clip({}, t) for t in titles]

    res = None
    with Capturing() as output:
        res = simple.downloader.print_titles(clips, simple.io, simple.filters)

    assert res == RD_SUCCESS
    assert output == titles


def test_print_metadata(simple):
    state = {}
    clips = [successful_clip(state)]

    res = None
    with Capturing() as output:
        res = simple.downloader.print_metadata(clips, simple.filters)
    metadata = json.loads('\n'.join(output))

    assert res == RD_SUCCESS
    assert 'command' not in state
    assert metadata == [
        {
            'webpage': 'https://areena.yle.fi/1-1234567',
            'title': 'Test clip: S01E01-2018-07-01T00:00',
            'flavors': [
                {
                    'media_type': 'video',
                    'height': 360,
                    'width': 640,
                    'bitrate': 880
                },
                {
                    'media_type': 'video',
                    'height': 720,
                    'width': 1280,
                    'bitrate': 1412
                },
                {
                    'media_type': 'video',
                    'height': 720,
                    'width': 1280,
                    'bitrate': 1872
                },
                {
                    'media_type': 'video',
                    'height': 1080,
                    'width': 1920,
                    'bitrate': 2808
                }
            ],
            'duration_seconds': 950,
            'subtitles': [
                {'url': 'https://example.com/subtitle/fin.srt', 'lang': 'fin'},
                {'url': 'https://example.com/subtitle/swe.srt', 'lang': 'swe'}
            ],
            'region': 'Finland',
            'publish_timestamp': '2018-07-01T00:00:00+03:00',
            'expiration_timestamp': '2019-01-01T00:00:00+03:00'
        }
    ]


def test_download_failed_clip(simple):
    clips = [failed_clip()]
    res = simple.downloader.download_episodes(
        clips, simple.io, simple.filters, None)

    assert res == RD_FAILED