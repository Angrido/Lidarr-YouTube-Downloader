# Metadata: multi-disc, MusicBrainz id frames

<!-- Moved verbatim from CLAUDE.md; keep it in sync with the code. -->

### Multi-disc albums

Lidarr's `trackNumber` restarts on every disc. On an album whose tracks span
more than one `mediumNumber`, `processing._track_number()` returns the
album-wide `absoluteTrackNumber` instead, and everything keyed on that number
follows: file names (`13 - Intro.mp3`), `temp_NN_` prefixes, the XML sidecar,
the `track_downloads.track_number` the history delete route rebuilds the file
name from, and the number `/api/download/queue/<id>/tracks` hands the manual
download. Tags keep the per-disc number and add the disc
(`metadata._disc_position()`: TPOS / `discnumber`+`disctotal` / `disk`).
Single-disc albums are unchanged.

The track total in `TRCK` / `trkn` / `tracknumber` comes from `metadata._track_total()`: an explicit `trackCount` in the album dict (the manual / playlist paths set one), else the number of tracks on that track's disc, else the monitored release's `trackCount`, else `statistics.trackCount`. Lidarr's album resource has **no top-level `trackCount`**, so reading only that key tagged every Lidarr download `N/0`.

### MusicBrainz id frames

Lidarr's track resource carries **two different** MusicBrainz ids, and they are
not interchangeable: `foreignRecordingId` identifies the recording, while
`foreignTrackId` identifies that recording's slot in one specific release's
tracklist. Writing the recording id into the release-track tag makes taggers
that validate it against the release tracklist reject the file outright
(issue #93), so `metadata._musicbrainz_fields()` is the single source of truth
for the mapping and both the MP3 and M4A paths go through it (`mp4=True`
selects the MP4 key names).

Frame names follow **Picard's** mapping, because that is what other taggers
read — `MusicBrainz Release Group Id` (not "Album Release Group Id") and
`MusicBrainz Album Release Country` (not "Release Country"). `_STALE_MB_DESCS`
lists the descriptions earlier versions got wrong; they are deleted before
writing so re-tagging an existing file cannot leave a mislabeled value behind.
For MP4 Picard stores the **recording** id under
`----:com.apple.iTunes:MusicBrainz Track Id` (the release-track id stays under
`MusicBrainz Release Track Id`); `_STALE_MP4_MB_DESCS` adds the old
`MusicBrainz Recording Id` key to the delete list.

Vorbis Comments (Opus) invert the ID3 naming: on disk `musicbrainz_trackid`
holds the **recording** id and `musicbrainz_releasetrackid` holds the
release-track id, so `tag_opus()` deliberately does not mirror `tag_mp3()`.
