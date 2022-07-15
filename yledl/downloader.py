import copy
import logging
import os
from attr import asdict
from .utils import sane_filename
from .backends import Subprocess
from .exitcodes import to_external_rd_code, RD_SUCCESS, RD_INCOMPLETE, \
    RD_FAILED, RD_SUBPROCESS_EXECUTE_FAILED
from .io import OutputFileNameGenerator
from .streamflavor import FailedFlavor


logger = logging.getLogger('yledl')


class YleDlDownloader:
    def __init__(self, extractor, geolocation):
        self.extractor = extractor
        self.geolocation = geolocation

    def download_clips(self, base_url, io, filters):
        def download(clip, downloader):
            if not downloader:
                logger.error(f'Downloading the stream at {clip.webpage} is not yet supported.')
                logger.error('Try --showurl')
                return RD_FAILED

            downloader.warn_on_unsupported_feature(io)

            outputfile = self.generate_output_name(clip.title, downloader, io)
            if not outputfile:
                return (RD_FAILED, None)
            if self.should_skip_downloading(outputfile, downloader, clip, io):
                logger.info(f'{outputfile} has already been downloaded.')
                return (RD_SUCCESS, outputfile)

            self.log_output_file(outputfile)
            dl_result = downloader.save_stream(outputfile, clip, io)

            if dl_result == RD_SUCCESS:
                self.log_output_file(outputfile, True)
                self.postprocess(io.postprocess_command, outputfile, [])

            return (dl_result, outputfile)

        def needs_retry(res):
            return res not in [RD_SUCCESS, RD_INCOMPLETE]

        playlist = self.extractor.get_playlist(base_url, filters.latest_only)

        if len(playlist) > 1 and io.outputfilename is not None:
            logger.error('The source is a playlist with multiple clips, '
                         'but only one output file specified')
            return RD_FAILED
        elif len(playlist) > 1 and self.extractor.title_formatter.is_constant_pattern():
            logger.error('The source is a playlist with multiple clips, '
                         'but --output-template is a literal: '
                         f'{self.extractor.title_formatter.template}')
            return RD_FAILED

        return self.process(playlist, download, needs_retry, filters)

    def should_skip_downloading(self, outputfile, downloader, clip, io):
        limits = io.download_limits
        slicing_active = limits.start_position or 0 > 0 or limits.duration

        return ((not io.overwrite and os.path.exists(outputfile)) or
                (not slicing_active and
                 downloader.full_stream_already_downloaded(outputfile, clip, io)))

    def generate_output_name(self, title, downloader, io):
        generator = OutputFileNameGenerator()
        extension = downloader.file_extension(io.preferred_format)
        return generator.filename(title, extension, io)

    def pipe(self, base_url, io, filters):
        playlist = self.extractor.get_playlist(base_url)

        # Can pipe one stream only. Drop other streams if there are more than one.
        playlist = playlist[:1]

        def pipe_clip(clip, downloader):
            if not downloader:
                logger.error(f'Downloading the stream at {clip.webpage} is not yet supported.')
                return RD_FAILED
            downloader.warn_on_unsupported_feature(io)
            res = downloader.pipe(io)
            return (res, None)

        def needs_retry(res):
            return res == RD_SUBPROCESS_EXECUTE_FAILED

        return self.process(playlist, pipe_clip, needs_retry, filters)

    def get_urls(self, base_url, filters):
        clips = self.extractor.extract(base_url, filters.latest_only)
        for clip in clips:
            streams = self.select_streams(clip.flavors, filters)
            if streams and any(s.is_valid() for s in streams):
                valid_stream = next(s for s in streams if s.is_valid())
                yield valid_stream.stream_url()

    def get_titles(self, base_url, latest_only, io):
        clips = self.extractor.extract(base_url, latest_only)
        return (sane_filename(clip.title or '', io.excludechars) for clip in clips)

    def get_metadata(self, base_url, latest_only, io):
        clips = self.extractor.extract(base_url, latest_only)
        return list(clip.metadata(io) for clip in clips)

    def process(self, playlist, streamfunc, needs_retry, filters):
        if len(playlist) == 0:
            logger.error('No streams found')
            return RD_SUCCESS

        overall_status = RD_SUCCESS
        for clip_url in playlist:
            clip = self.extractor.extract_clip(clip_url)
            streams = self.select_streams(clip.flavors, filters)

            if not streams:
                logger.error('No stream found')
                overall_status = RD_FAILED
            elif all(not stream.is_valid() for stream in streams):
                logger.error(f'Unsupported stream: {streams[0].error_message}')
                self.print_geo_warning(clip)

                overall_status = RD_FAILED
            else:
                res = self.try_all_streams(streamfunc, clip, streams, needs_retry)
                if res != RD_SUCCESS and overall_status != RD_FAILED:
                    overall_status = res

        return to_external_rd_code(overall_status)

    def try_all_streams(self, streamfunc, clip, streams, needs_retry):
        latest_result = RD_FAILED
        output_file = None
        for stream in streams:
            if stream.is_valid():
                # Remove if there is a partially downloaded file from the
                # earlier failed stream
                if output_file:
                    self.remove_retry_file(output_file)

                logger.debug(f'Now trying downloader {stream.name}')

                (latest_result, output_file) = streamfunc(clip, stream)
                if needs_retry(latest_result):
                    continue

                return latest_result

        return latest_result

    def select_flavor(self, flavors, filters):
        if not flavors:
            return None

        logger.debug('Available flavors:')
        for fl in flavors:
            logger.debug('bitrate: {bitrate}, height: {height}, '
                         'width: {width}'
                         .format(**asdict(fl)))
        logger.debug('max_height: {maxheight}, max_bitrate: {maxbitrate}'
                     .format(**asdict(filters)))

        filtered = self.apply_backend_filter(flavors, filters)
        filtered = self.apply_resolution_filters(filtered, filters)

        if filtered:
            selected = filtered[-1]
            logger.debug(f'Selected flavor: {selected}')
        else:
            selected = None

        return selected

    def apply_backend_filter(self, flavors, filters):
        def filter_streams_by_backend(flavor):
            sorted_streams = []
            for be in filters.enabled_backends:
                for downloader in flavor.streams:
                    if downloader.name == be:
                        sorted_streams.append(downloader)

            res = copy.copy(flavor)
            res.streams = sorted_streams
            return res

        if not flavors:
            return []

        filtered = [filter_streams_by_backend(fl) for fl in flavors]
        filtered = [fl for fl in filtered if fl.streams]

        if filtered:
            return filtered
        elif flavors:
            return [self.backend_not_enabled_flavor(flavors)]
        else:
            return []

    def apply_resolution_filters(self, flavors, filters):
        def sort_max_bitrate(x):
            return x.bitrate or 0

        def sort_max_resolution_min_bitrate(x):
            return (x.height or 0, -(x.bitrate or 0))

        def sort_max_resolution_max_bitrate(x):
            return (x.height or 0, x.bitrate or 0)

        filtered = [
            fl for fl in flavors
            if (filters.maxbitrate is None or
                (fl.bitrate or 0) <= filters.maxbitrate) and
            (filters.maxheight is None or
             (fl.height or 0) <= filters.maxheight)
        ]

        if filtered:
            acceptable_flavors = filtered
            reverse = False
        else:
            acceptable_flavors = flavors
            reverse = filters.maxheight is not None or filters.maxbitrate is not None

        if filters.maxheight is not None and filters.maxbitrate is not None:
            keyfunc = sort_max_resolution_max_bitrate
        elif filters.maxheight is not None:
            keyfunc = sort_max_resolution_min_bitrate
        else:
            keyfunc = sort_max_bitrate

        return sorted(acceptable_flavors, key=keyfunc, reverse=reverse)

    def backend_not_enabled_flavor(self, flavors):
        supported_backends = set()
        for fl in flavors:
            supported_backends.update(
                s.name for s in fl.streams if s.is_valid())

        error_messages = [s.error_message
                          for fl in flavors
                          for s in fl.streams if not s.is_valid()]

        if supported_backends:
            msg = f'Required backend not enabled. Try: --backend {",".join(supported_backends)}'
        elif error_messages:
            msg = error_messages[0]
        else:
            msg = 'Stream not found'

        return FailedFlavor(msg)

    def error_flavor(self, flavors):
        for fl in flavors:
            for s in fl.streams:
                if not s.is_valid():
                    return FailedFlavor(s.error_message)

        return None

    def select_streams(self, flavors, filters):
        flavor = self.select_flavor(flavors, filters)
        if flavor:
            return flavor.streams or []
        else:
            return None

    def print_geo_warning(self, clip):
        if (
            clip.region in ['Finland', None] and
            not self.geolocation.located_in_finland(clip.webpage)
        ):
            logger.error('This clip is only available in Finland '
                         'and according to Yle you are located abroad')

    def log_output_file(self, outputfile, done=False):
        if outputfile and outputfile != '-':
            if done:
                logger.info(f'Stream saved to {outputfile}')
            else:
                logger.info(f'Output file: {outputfile}')

    def remove_retry_file(self, filename):
        if filename and os.path.isfile(filename):
            logger.debug('Removing the partially downloaded file')
            try:
                os.remove(filename)
            except OSError:
                logger.warn('Failed to remove a partial output file')

    def postprocess(self, postprocess_command, videofile, subtitlefiles):
        if postprocess_command:
            args = [postprocess_command, videofile]
            args.extend(subtitlefiles)
            return Subprocess().execute([args], None)
