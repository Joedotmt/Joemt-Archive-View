"""Seek to the frames needed for a low-fps video preview.

Runs as a child process so the normal preview watchdog can interrupt blocked I/O.
An unsupported layout or inaccurate seek lets the caller retry with FFmpeg.
"""

from contextlib import ExitStack
from fractions import Fraction
import json
from math import ceil, floor
import struct
import sys

import av

if __package__:
    from .shared_video import open_shared_video
else:
    from shared_video import open_shared_video


MAX_KEYFRAME_OFFSET_SECONDS = 2


def _visible_area(video):
    """Apply the clean aperture and orthogonal display rotation before scaling."""
    side_data = video.codec_context.coded_side_data
    if side_data.keys() - {"frame_cropping", "cpb_properties", "display_matrix"}:
        raise ValueError("Use FFmpeg for additional presentation metadata.")
    filters = []
    width, height = video.width, video.height
    crop = side_data.get("frame_cropping")
    if crop:
        top, bottom, left, right = struct.unpack("<4I", crop)
        width -= left + right
        height -= top + bottom
        if min(width, height) < 2:
            raise ValueError("Invalid clean aperture.")
        filters.append(("crop", f"{width}:{height}:{left}:{top}:exact=1"))
    swap_axes = False
    matrix = side_data.get("display_matrix")
    if matrix:
        a, b, u, c, d, v, x, y, w = struct.unpack("<9i", matrix)
        unit = 1 << 16
        if (u, v, x, y, w) != (0, 0, 0, 0, 1 << 30):
            raise ValueError("Unsupported display transform.")
        if (a, b, c, d) in ((0, -unit, unit, 0), (0, unit, -unit, 0)):
            filters.append(("transpose", "cclock" if b < 0 else "clock"))
            width, height = height, width
            swap_axes = True
        elif b == c == 0 and abs(a) == abs(d) == unit:
            if a < 0:
                filters.append(("hflip", None))
            if d < 0:
                filters.append(("vflip", None))
        else:
            raise ValueError("Unsupported display transform.")
    return width, height, swap_axes, filters


def _filter_graph(frame, filters):
    graph = av.filter.Graph()
    graph.threads = 2
    graph.link_nodes(
        graph.add("buffer", video_size=f"{frame.width}x{frame.height}",
                  pix_fmt=frame.format.name, time_base=str(frame.time_base),
                  colorspace=str(frame.colorspace), range=str(frame.color_range)),
        *(graph.add(name, args) for name, args in filters),
        graph.add("buffersink"),
    )
    graph.configure()
    return graph


def _stream_duration(original, video):
    if video.duration:
        return video.duration
    # Matroska and some transport streams publish only a container duration.
    # Locate the actual end of the video track (the audio can end later).
    if not original.duration:
        raise ValueError("Missing duration.")
    near_end = video.start_time + int((Fraction(original.duration, av.time_base) - 1) / video.time_base)
    original.seek(max(video.start_time, near_end), stream=video)
    last = None
    for frame in original.decode(video):
        last = frame
    if last is None or last.pts is None or last.duration <= 0:
        raise ValueError("Uncertain video end time.")
    return last.pts + last.duration - video.start_time


def _nearby_keyframe(original, video, target, packets, previous):
    """Choose by display timestamp; an index may instead contain decode times."""
    if not packets or not min(packets) <= target <= max(packets):
        packets.clear()
        for backward in (True, False):
            try:
                original.seek(target, stream=video, backward=backward)
                packet = next(original.demux(video), None)
            except av.FFmpegError:
                continue  # There may be no following keyframe near EOF.
            if (packet is not None and packet.is_keyframe and not packet.is_corrupt
                    and not packet.is_discard and packet.pts is not None
                    and packet.pts >= video.start_time
                    and (video.duration is None or packet.pts < video.start_time + video.duration)):
                packets[packet.pts] = packet
    for pts in sorted(packets, key=lambda t: (abs(t - target), t)):
        if abs(pts - target) * video.time_base > MAX_KEYFRAME_OFFSET_SECONDS:
            break
        if previous is not None:
            if pts < previous.pts:
                continue
            if pts == previous.pts:
                return previous
        video.codec_context.flush_buffers()
        frames = packets[pts].decode()
        if not frames:
            frames = video.decode(None)
        for frame in frames:
            if frame.key_frame and not frame.is_corrupt and frame.pts == pts:
                return frame
    return None


def _sample_frames(original, video, fps):
    rate = video.average_rate or video.guessed_rate
    tb = video.time_base
    start = video.start_time
    if not rate or rate <= fps or not tb or start is None or original.start_time is None:
        raise ValueError("Missing or unsuitable sampling timeline.")
    if abs(start * tb * av.time_base - original.start_time) > 1:
        raise ValueError("Video and container have different start times.")
    duration = _stream_duration(original, video)
    count = floor(duration * tb * fps + Fraction(1, 2))
    if count <= 0:
        raise ValueError("Clip shorter than a preview frame.")
    regular = rate == video.base_rate and video.frames == duration * tb * rate
    interval = 1 / (fps * tb)
    entries = video.index_entries
    intra = video.codec_context.codec.intra_only
    use_nearby = False
    if not intra and len(entries):
        # Estimate the input we can skip at representative output times. With
        # long GOPs the normal threaded FFmpeg pass is usually more efficient.
        indices = {i * (count - 1) // 7 for i in range(8)}
        skipped = 0
        for i in indices:
            target = start + min(duration - 1, ceil((i + Fraction(1, 2)) * interval) - 1)
            pos = entries.search_timestamp(target)
            if pos >= 0:
                skipped += max(0, interval - (target - entries[pos].timestamp))
        use_nearby = skipped < len(indices) * interval * Fraction(3, 4)
    packets = {}
    previous = None
    for index in range(count):
        # FFmpeg's fps filter keeps the last frame strictly before the
        # half-slot boundary. Rational timestamps avoid drift at NTSC rates.
        boundary = (index + Fraction(1, 2)) / fps
        if regular:
            number = min(video.frames - 1, ceil(boundary * rate) - 1)
            target = start + round(number / (rate * tb))
        else:
            target = start + min(duration - 1, ceil(boundary / tb) - 1)
        frame = _nearby_keyframe(original, video, target, packets, previous) if use_nearby else None
        if frame is None:
            frame = _exact_frame(original, video, target, interval, regular)
            if frame is None and not intra and not use_nearby:
                # Some containers have no index during preflight. The actual
                # seek can still find independent frames efficiently.
                use_nearby = True
                frame = _nearby_keyframe(original, video, target, packets, previous)
            elif frame is not None and regular and frame.duration and (
                frame.pts + frame.duration < start + min(duration, ceil(boundary / tb))
            ):
                raise ValueError("Irregular frame duration on a nominally regular timeline.")
        if frame is None or (previous is not None and frame.pts < previous.pts):
            raise ValueError("No suitable frame at the requested time.")
        previous = frame
        yield frame


def _exact_frame(original, video, target, interval, regular):
    # MPEG transport and reordered tracks can initially land *after* the
    # requested timestamp. Retry from an earlier packet in that case.
    for anchor in (target, max(video.start_time, target - ceil(interval / 2))):
        original.seek(anchor, stream=video)
        selected = None
        for frame in original.decode(video):
            if frame.pts is None or frame.is_corrupt:
                raise ValueError("Missing timestamp or damaged frame.")
            if frame.pts > target:
                break
            selected = frame
            if frame.pts == target:
                break
            if target - frame.pts > interval:
                return None
        else:
            if selected is not None and selected.pts + selected.duration <= target:
                raise ValueError("Video ended before the requested sample.")
        if selected is not None and (not regular or selected.pts == target):
            return selected
    return None


def sample_preview(source, destination, fps, max_height, crf, preset):
    with av.open(source) as original:
        video = original.streams.video[0]
        source_size = video.width, video.height
        visible_width, visible_height, swap_axes, filters = _visible_area(video)
        height = min(visible_height, max_height) // 2 * 2
        width = 2 * floor(Fraction(visible_width * height, 2 * visible_height) + Fraction(1, 2))
        aspect = video.sample_aspect_ratio or Fraction(0, 1)
        if aspect and swap_axes:
            aspect = 1 / aspect
        aspect *= Fraction(visible_width * height, visible_height * width)
        video.codec_context.thread_count = 4
        with av.open(destination, "w", format="mp4", options={"movflags": "+faststart"}) as output:
            proxy = output.add_stream("libx264", rate=fps)
            proxy.width, proxy.height = width, height
            proxy.codec_context.sample_aspect_ratio = aspect
            proxy.pix_fmt = "yuv420p"
            proxy.options = {"preset": preset, "crf": crf}
            for index, frame in enumerate(_sample_frames(original, video, fps)):
                if ((frame.width, frame.height) != source_size
                        or frame.color_trc in (16, 18)):
                    raise ValueError("Use FFmpeg for this image format or display transform.")
                if "DISPLAYMATRIX" in frame.side_data and (
                    bytes(frame.side_data["DISPLAYMATRIX"]) != video.codec_context.coded_side_data.get("display_matrix")
                ):
                    raise ValueError("Display matrix changed during the clip.")
                if index == 0:
                    filters.extend([("scale", f"{width}:{height}"), ("format", "pix_fmts=yuv420p")])
                    graph = _filter_graph(frame, filters)
                    initial_format = (frame.format.name, frame.colorspace, frame.color_range,
                                      frame.color_primaries, frame.color_trc)
                elif (frame.format.name, frame.colorspace, frame.color_range,
                      frame.color_primaries, frame.color_trc) != initial_format:
                    raise ValueError("Source format changed during the clip.")
                graph.push(frame)
                frame = graph.pull()
                if index == 0:
                    proxy.color_range, proxy.colorspace = frame.color_range, frame.colorspace
                    proxy.color_primaries, proxy.color_trc = frame.color_primaries, frame.color_trc
                frame.pict_type = 0
                frame.pts = index
                frame.time_base = 1 / fps
                output.mux(proxy.encode(frame))
                print(f"out_time_us={int((index + 1) * 1_000_000 / fps)}", flush=True)
            output.mux(proxy.encode())
    print("progress=end", flush=True)


def _probe(source):
    with av.open(source) as original:
        streams = []
        for stream in original.streams:
            ctx = stream.codec_context
            duration = None
            if stream.duration is not None and stream.time_base:
                duration = float(stream.duration * stream.time_base)
            codec = ctx.codec if ctx else None
            streams.append(dict(
                codec_type=stream.type, codec_name=codec.canonical_name if codec else None,
                duration=duration, bit_rate=getattr(ctx, "bit_rate", None),
                width=getattr(ctx, "width", None), height=getattr(ctx, "height", None),
                sample_rate=getattr(ctx, "sample_rate", None), channels=getattr(ctx, "channels", None),
            ))
        return dict(streams=streams, format=dict(
            format_name=original.format.name, bit_rate=original.bit_rate,
            duration=original.duration / av.time_base if original.duration is not None else None,
        ))


def main(arguments):
    try:
        with ExitStack() as inputs:
            if arguments[:1] == ["--shared-video"]:
                _, name, size, *arguments = arguments
                source = inputs.enter_context(open_shared_video(name, int(size)))
            else:
                source, *arguments = arguments
            if arguments == ["--probe"]:
                print(json.dumps(_probe(source)))
            else:
                destination, fps, height, crf, preset = arguments
                sample_preview(source, destination, Fraction(fps), int(height), crf, preset)
    except Exception as error:
        print(f"Video sampling failed: {error}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
