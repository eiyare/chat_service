# saaschat for Moodle (`local_saaschat`)

Adds the saaschat assistant to every Moodle page, greets signed-in users by name,
keeps their conversations across devices, answers students from the course they are in,
and can keep a copy of the workspace's document passages in the Moodle database.

Moodle 4.0 or later.

## saaschat side (Django admin → Website widgets → the workspace)

1. Make sure the widget is enabled and the Moodle site's domain is in **Allowed websites**.
2. Under **Moodle**, tick **Moodle integration** and save. A **Shared code** appears.
3. Choose **Chunk storage**: *saaschat only*, or *saaschat + copy in Moodle*.
4. To answer from course content, tick **Course content**.
5. Put `{name}` in the greeting where the user's name should go, e.g. `Hi {name}! …`
   (visitors who aren't signed in see "Guest").

## Moodle side

1. Zip the `local_saaschat` folder and install it: Site administration → Plugins → Install plugins.
2. Site administration → Plugins → Local plugins → saaschat: enter the **widget key** (the
   `data-key` from the workspace's Admin → Embed page) and the **shared code**.
3. Remove any saaschat `<script>` previously pasted into Appearance → Additional HTML,
   or the button will appear twice.

## Course-aware answers

In the plugin settings, choose **Course content**: *All visible courses* or *Courses listed below*
(with their IDs). Every hour the scheduled task "Send course content to saaschat" sends each
chosen course's:

- overview and schedule: course dates, section names and summaries, labels, and each activity
  with its open/due/close dates;
- activity descriptions, Page and Book text, assignment instructions and URL addresses;
- files in File and Folder activities (the types the workspace accepts, within its size limit);
- forum posts, newest discussions first (no Q&A forums, private replies, separate-group
  discussions or timed posts outside their display period).

Only what every student can see is sent: hidden or restricted sections and activities, and
activities not shown on the course page, are left out. Quiz questions are never sent. Only
changed items are sent again; items removed in Moodle, and courses no longer chosen, are deleted
in saaschat.

On the pages of a chosen course, the assistant answers signed-in students from that course's
content only (not the workspace's documents or FAQs), and keeps a separate conversation for each
course. Elsewhere in Moodle it answers from the workspace's documents as before. Dates are the
course's default dates; per-student extensions and overrides are not reflected.

To send the content now instead of waiting:
`php admin/cli/scheduled_task.php --execute='\local_saaschat\task\sync_courses'`

## Passage copy

With *saaschat + copy in Moodle*, the scheduled task "Copy saaschat document passages" fills
the `local_saaschat_chunks` table every hour. Search still runs in saaschat.

To rotate the shared code, use the "Regenerate the Moodle shared code" action in Django admin
and paste the new code into the plugin settings.
