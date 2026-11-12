"""Encode sharp walkthroughs with one stable palette and exact color matching."""
from pathlib import Path
import json
import sys

from PIL import Image, ImageChops, ImageSequence, ImageStat

# Keep the complete 80ms-sampled story; play each sample in 60ms for a 5.4s loop.
SAMPLE_FRAME_MS = 80
PLAYBACK_FRAME_MS = 60


def quantize_frames(images):
    contact = Image.new('RGB', (720 * 6, 360 * 4), 'white')
    for index, image in enumerate(images[::4]):
        contact.paste(image, ((index % 6) * 720, (index // 6) * 360))
    palette_image = contact.quantize(colors=256, method=Image.Quantize.MEDIANCUT)
    palette = palette_image.getpalette()
    colors = {}
    for index in range(len(palette) // 3):
        colors.setdefault(tuple(palette[index * 3:index * 3 + 3]), index)

    def tree(entries, depth=0):
        if not entries:
            return None
        axis = depth % 3
        entries.sort(key=lambda entry: entry[0][axis])
        middle = len(entries) // 2
        return (entries[middle], axis, tree(entries[:middle], depth + 1), tree(entries[middle + 1:], depth + 1))

    root = tree(list(colors.items()))

    def nearest(color, node, best=(float('inf'), 0)):
        if node is None:
            return best
        (candidate, index), axis, left, right = node
        distance = sum((a - b) ** 2 for a, b in zip(color, candidate))
        if distance < best[0] or distance == best[0] and index < best[1]:
            best = (distance, index)
        delta = color[axis] - candidate[axis]
        near, far = (left, right) if delta < 0 else (right, left)
        best = nearest(color, near, best)
        return nearest(color, far, best) if delta * delta <= best[0] else best

    # Pillow's shared-palette conversion uses a coarse color lookup. Match each
    # unique RGB color exactly instead, retaining one palette across all frames.
    cache = dict(colors)
    frames = []
    for image in images:
        for _, color in image.getcolors(image.width * image.height):
            if color not in cache:
                cache[color] = nearest(color, root)[1]
        indexed = Image.frombytes('P', image.size, bytes(map(cache.__getitem__, image.getdata())))
        indexed.putpalette(palette)
        frames.append(indexed)
    return frames


def main():
    source, target, artifact = map(Path, sys.argv[1:])
    images = [Image.open(file).convert('RGB') for file in sorted(source.glob('*.png'))]
    assert len(images) == 90
    assert all(image.size == (720, 360) for image in images)
    assert ImageChops.difference(images[0], images[73]).getbbox() is None, 'Opening must match the static poster'
    assert ImageChops.difference(images[0], images[-1]).getbbox() is None, 'Loop must return to the same cover'

    frames = quantize_frames(images)
    frames[0].save(target, save_all=True, append_images=frames[1:], duration=PLAYBACK_FRAME_MS, loop=0, optimize=True, disposal=1)
    with Image.open(target) as gif:
        duration = sum(frame.info.get('duration', 0) for frame in ImageSequence.Iterator(gif))
        assert duration == len(images) * PLAYBACK_FRAME_MS and gif.info['loop'] == 0 and gif.size == (720, 360)
        assert gif.n_frames >= 20
        count = gif.n_frames
        gif.seek(0)
        first = gif.convert('RGB').copy()
        gif.seek(count - 1)
        assert ImageChops.difference(first, gif.convert('RGB')).getbbox() is None, 'Decoded GIF loop must not jump'
        poster_error = round(sum(ImageStat.Stat(ImageChops.difference(first, images[73])).mean) / 3, 4)
        assert poster_error < .8, poster_error

    story = Image.new('RGB', (720, 360 * 3 + 16 * 2), '#edf3f9')
    for index, milliseconds in enumerate([1120, 2960, 5840]):
        image = images[milliseconds // SAMPLE_FRAME_MS]
        image.save(artifact / f'{target.stem}-stage-{index + 1}.png')
        story.paste(image, (0, index * 376))
    story.save(artifact / f'{target.stem}-story.png')
    images[73].save(artifact / f'{target.stem}-poster.png')
    differences = [round(sum(ImageStat.Stat(ImageChops.difference(images[a], images[b])).mean) / 3, 3)
                   for a, b in [(14, 37), (37, 73), (14, 73)]]
    assert min(differences) > .8, differences
    print(json.dumps({'id': target.stem, 'bytes': target.stat().st_size, 'width': 720, 'height': 360,
                      'duration_ms': duration, 'frames': count, 'scene_differences': differences,
                      'poster_mean_error': poster_error, 'loop_pixel_identical': True}))


if __name__ == '__main__':
    main()
