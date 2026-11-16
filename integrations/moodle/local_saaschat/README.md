# saaschat for Moodle (`local_saaschat`)

Adds the saaschat assistant to every Moodle page, greets signed-in users by name,
keeps their conversations across devices, and can keep a copy of the workspace's
document passages in the Moodle database.

Moodle 4.0 or later.

## saaschat side (Django admin → Website widgets → the workspace)

1. Make sure the widget is enabled and the Moodle site's domain is in **Allowed websites**.
2. Under **Moodle**, tick **Moodle integration** and save. A **Shared code** appears.
3. Choose **Chunk storage**: *saaschat only*, or *saaschat + copy in Moodle*.
4. Put `{name}` in the greeting where the user's name should go, e.g. `Hi {name}! …`
   (visitors who aren't signed in see "Guest").

## Moodle side

1. Zip the `local_saaschat` folder and install it: Site administration → Plugins → Install plugins.
2. Site administration → Plugins → Local plugins → saaschat: enter the **widget key** (the
   `data-key` from the workspace's Admin → Embed page) and the **shared code**.
3. Remove any saaschat `<script>` previously pasted into Appearance → Additional HTML,
   or the button will appear twice.

With *saaschat + copy in Moodle*, the scheduled task "Copy saaschat document passages" fills
the `local_saaschat_chunks` table every hour. Search still runs in saaschat.

To rotate the shared code, use the "Regenerate the Moodle shared code" action in Django admin
and paste the new code into the plugin settings.
