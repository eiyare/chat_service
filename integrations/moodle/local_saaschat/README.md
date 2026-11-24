# saaschat for Moodle (`local_saaschat`)

Adds the saaschat assistant to Moodle pages (all of them, or the areas you choose), greets signed-in users by name,
keeps their conversations across devices, answers students from the course they are in,
and can keep a copy of the workspace's document passages in the Moodle database.

Moodle 4.0 or later.

## saaschat side (the workspace's Admin)

1. **Embed** tab: make sure the widget is enabled and the Moodle site's domain is in **Allowed
   websites**. Put `{name}` in the greeting where the user's name should go, e.g. `Hi {name}! …`
   (visitors who aren't signed in see "Guest").
2. **Integrations** tab → **Connect an app**: type *Moodle*. Its page shows the widget key and the
   **shared code** to enter in Moodle.
3. On that page, tick what Moodle may do:
   - **Signed-in users**: students greeted by name, with their own conversations;
   - **Content from the app**: course content, so students get answers from their course;
   - **Tools**: look up students' own grades, completion and deadlines (see below);
   - **Let the app copy passages**: the passage copy into Moodle.

(Moodle is one of several apps a workspace can connect; see `docs/integrations.md`.)

## Moodle side

1. Zip the `local_saaschat` folder and install it: Site administration → Plugins → Install plugins.
2. Site administration → Plugins → Local plugins → saaschat: enter the **widget key** (the
   `data-key` from the workspace's Admin → Embed page) and the **shared code**.
3. Remove any saaschat `<script>` previously pasted into Appearance → Additional HTML,
   or the button will appear twice.

## Where the assistant appears

By default on every page. Under **Show the assistant on**, untick the areas where it shouldn't
appear: Site home, Dashboard and My courses, course categories, course main pages, activities and
resources, quiz attempts, user profiles, reports and grades, site administration, the login page,
and all other pages. Quiz attempts are the pages where a student answers a quiz (and Safe Exam
Browser pages); a quiz's info page counts as an activity page.

For finer control, list Moodle page types under **Hide on these page types**, one per line, e.g.
`mod-assign-view`, or `mod-quiz-*` for every quiz page. A page's type is the `page-…` id on its
`<body>` tag, without `page-`.

## Course-aware answers

In the plugin settings, choose **Course content**: *All visible courses* or *Courses listed below*
(with their IDs). Every hour the scheduled task "Send course content to saaschat" sends each
chosen course's:

- overview and schedule: course dates, how many activities and resources of each type there are,
  section names and summaries, labels, and each activity with its open/due/close dates;
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
course. On an activity's pages it also knows which activity the student is viewing, so questions
like "what is this module about?" are answered from that activity. Elsewhere in Moodle it answers
from the workspace's documents as before. Dates are the course's default dates; per-student
extensions and overrides are not reflected.

To send the content now instead of waiting:
`php admin/cli/scheduled_task.php --execute='\local_saaschat\task\sync_courses'`

## Students' own grades, completion and deadlines

With **Tools** ticked on the Moodle integration in saaschat and **Students' own grades, completion and deadlines**
ticked in the plugin settings, the assistant on a course's pages can answer questions such as
"what is my grade?", "what do I still need to complete this course?" or "what's due this week?".
It decides for itself what to look up, possibly several things in turn, from these tools:

- **get_my_grades**: the student's grade report: each item they can see with its grade, range,
  percentage, weight and feedback, the course total, and how much of the course weight is graded;
- **get_my_completion**: the course's completion criteria and which are met, and each tracked
  activity with its status and what it requires;
- **get_my_deadlines**: what they still have to do, with due dates including their own extensions
  and overrides, and whether each is overdue;
- **search_course**: the course content, as for other questions.

How the lookups are protected:

- saaschat calls `local/saaschat/student.php` on this Moodle with the shared code **and the
  student's own signed sign-in token** from the page they are chatting on, so it can only look up
  the student who is asking, and only while they are signed in (tokens last 12 hours).
- The lookup runs as that student, so Moodle decides what they may see: hidden and not yet released
  grades stay hidden, and "Show gradebook to students" switched off in a course means no grades.
- saaschat only calls back to a Moodle address that is one of the widget's **Allowed websites**.
- The looked-up data goes to the chat model only. saaschat's Brain map records which tool was used
  and a summary ("3 grade items, course total included"), not the grades. The assistant's reply,
  like every reply, is kept in the student's conversation.

The chat model must support tool calling (OpenAI, Anthropic, Gemini and most hosted models do;
some local models don't). If it rejects tools, the assistant answers without them, and the Brain
map says so. Completion answers need completion tracking turned on in the course.

## Passage copy

With *saaschat + copy in Moodle*, the scheduled task "Copy saaschat document passages" fills
the `local_saaschat_chunks` table every hour. Search still runs in saaschat.

To rotate the shared code, use "Generate a new secret" on the Moodle integration's page (Admin →
Integrations) and paste the new code into the plugin settings.
