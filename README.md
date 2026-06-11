# Teacher Coordination System
#### Video Demo: https://youtu.be/ksOqM7UHSxA

## What it is

The Teacher Coordination System is a web application built for small schools to manage substitute teacher requests. When a teacher cannot make it to class, they log in, report their absence for a specific date and slot, and the system automatically identifies eligible substitute teachers and emails them a request. The first teacher to accept is confirmed, and both the absent teacher and the substitute receive email confirmations (which can be cancelled by both the substitute and the main teacher). The system also provides a full timetable view with grade and/or teacher filters, a personal schedule for each teacher, an admin panel for managing the school, and a substitution log for oversight.

The project was built over approximately one month for my CS50x final project, using Python, Flask, PostgreSQL, and Bootstrap 5.

## Why it was built

The initial motivation was a specific school, where substitute coordination was being handled manually over WhatsApp. Teachers had no reliable way to know who was available, and admins had no record of what had been covered and by whom. Moreover, despite the large pool of substitute teachers available, the absent teachers tended to call on the more 'familiar' teachers, which put on significantly more work for some of the substitutes.

Two weeks into development, it became clear that several other small schools faced a similar problem. This shifted the design philosophy: rather than hardcoding anything specific to the initial school, the system was made configurable through the admin panel. The school name, logo, theme colours, school hours, grades, and subjects are all manageable by the admin without touching any code.  

## A note on AI assistance

This project was built with the assistance of Claude (Anthropic). The overall architecture, database schema, business logic, routing, and design decisions were driven by me throughout. Claude assisted primarily with the frontend styling and CSS, the `load_schedule` function in `helpers.py` which generates dated FullCalendar events from the database, and general debugging. Claude illustrated the owl SVG used in error pages. All code written by Claude was reviewed, understood, edited, and integrated by me. Claude also assisted in compiling my clustered notes throughout the development into this README to ensure all features are explained clearly, and the README was then reviewed and edited by me.

## Primary philosophy

The whole web app was built with one idea in mind - the teachers should be able to easily adopt this new system into their school. This meant:
1. Making intuitive design decisions
2. Creating reliability before adding additional features

With this in mind, there are only three types of users for this web app. The admin, the main teachers, and the substitute teachers.

## Core features (MVP)

- Teacher registration with subject and grade selection
- Fixed weekly timetable stored in PostgreSQL, displayed via FullCalendar
- Personal schedule view per teacher (only their own classes)
- Full timetable view with grade and teacher filters
- Absence reporting for a specific date and slot, limited to dates when the teacher actually has classes
- Minimum two-day advance notice required for absence reporting
- Automated eligibility check before inserting any database record. Finds teachers who can cover the subject and grade, and are not already teaching at that time
- Email notifications via Gmail SMTP with tokenised accept links
- Two-step substitution confirmation to prevent accidental accepts
- Confirmed substitutions overlaid on the timetable in a different colour for both the main and sub teachers.
- Absence cancellation with a two-day deadline, with email notifications to the substitute
- Substitute cancellation with a two-day deadline, automatically re-sending the request to other eligible teachers
- My Substitutions page for teachers to view and manage classes they have agreed to cover
- Admin panel for managing grades, subjects, timetable slots, and school settings. Visible only to set admins.
- School branding: logo upload, school name, theme colours, school hours. All configurable without code
- Teacher directory with subject/grade matrix and classes-covered count
- Substitution logs for admin oversight with summary statistics

## Additional features

- Dynamic school-wide theming via CSS variables controlled by the admin
- Flatpickr time picker for a better timetable editing experience
- Easter egg: press Z on your keyboard three times anywhere on the site
- Friendly error pages with an illustrated owl SVG
- Flash message confirmations for profile updates
- Admin logs page replacing email notifications to admin for substitution requests
- Teacher dropdown in timetable editor filtered dynamically by subject eligibility

## ENV file

In the project root, an ENV file with the following details is required:
GMAIL_ADDRESS=
GMAIL_APP_PASSWORD=
BASE_URL=
DB_HOST=
DB_NAME=
DB_USER=
DB_PASSWORD=

## Files

- **`app.py`** - The main Flask application. Contains all routes: authentication, timetable views, absence reporting, substitution acceptance and cancellation, profile management, substitute cancellation, and the full admin panel including grades, subjects, timetable editor, school settings, and logs.
- **`helpers.py`** - Utility functions used across the app: the `login_required` and `admin_required` decorators, the `load_schedule` function that generates FullCalendar events from the database (written by Claude (Anthropic)), email sending functions for substitution requests, confirmations, cancellations, substitute cancellations, and account deletion notices, and the `error` helper for rendering friendly error pages.
- **`templates/`** - All Jinja2 HTML templates. `layout.html` is the base template containing the navbar, dynamic theme variables, and footer. Each page has its own template. Admin-specific templates live in `templates/admin/`. The layout template design is inspired by CS50's Finance problem set.
- **`static/styles.css`** - Custom CSS built on top of Bootstrap 5. Uses CSS variables (`--primary`, `--secondary`, `--accent`) that are injected dynamically from the database, allowing the admin to retheme the entire site through the settings panel. Written by Claude (Anthropic).
- **`static/owl_error.svg`** - An inline SVG illustration of an owl with a magnifying glass, displayed on error pages. The owl image was created by Claude (Anthropic).
- **`teacher-coordination.env`** - Environment variables file containing database credentials, Gmail SMTP credentials, and the base URL. Never committed to version control.

## Design decisions

**From CSV to database**

The first approach was a Google Sheets timetable downloaded as a CSV, parsed with pandas, and converted to FullCalendar format. This worked initially but became a bottleneck quickly. The substitution logic requires answering questions like "which teachers can cover X subject for Grade Y at Z time?", which is a query, not a spreadsheet scan. Importing the CSV data into a database table was considered, but ultimately the CSV approach was retired for it's unreliability - the timetable would have to be made in a very specific format, which would open a whole other can of worms.

Switching to PostgreSQL meant the timetable became a set of structured records that could be filtered and queried reliably. However, another major part of the app's philosophy was to make it as straightforward as possible for the teachers. To find the middle ground, the admin was given a timetable editor in the web app instead. This added a small learning curve for the admin, but ultimately was worth it for its reliability, as it removed the dependency on external tools entirely.


**Recurring events vs. dated events**

FullCalendar supports recurring events (repeat every Monday) which seemed like the natural fit for a fixed weekly timetable, and so was the first approach. However, once substitutions needed to be overlaid on specific dates, the recurring model became a problem. FullCalendar has no simple way to say "show this recurring event every week except this one specific date."

The solution was to generate individual dated events on the backend for the full school year window. Every occurrence of every slot becomes its own event with a specific date. This makes the substitution overlay much easier - if a substitution record exists for that slot on that date, change the colour. 


**First-come-first-served substitution**

When a teacher reports an absence, emails go out to all eligible substitute teachers simultaneously. The first to accept gets the slot. This is enforced at the database level using a `WHERE status = 'pending'` condition in the UPDATE query with a `RETURNING id` clause. If two teachers click accept at the same millisecond, PostgreSQL processes them sequentially and only one gets the confirmation back. The second is shown a "too slow" page. This was not a foreseen issue until I remembered the milk analogy from Professor David Malan's lecture :)


**Confirmation step before accepting**

An earlier version of the accept flow locked the substitution as soon as the teacher clicked the email link. This was changed to a two-step flow: the link shows a confirmation page with the class details, and a second click confirms. This prevents accidental accepts from teachers who were just checking the details.


**Cancellation deadlines**

Both absence reporting and substitution cancellation enforce a two-day rule. Teachers must report absences at least two days in advance, as this gives eligible substitutes enough time to prepare. Similarly, a confirmed substitute can cancel their commitment up to two days before the class, after which the slot is locked and requires contacting the admin for manual reporting. Currently, there is no feature for even the admin to bypass the two day rule. When a substitute cancels within the deadline, the system automatically re-sends the request to all other eligible teachers so the absent teacher is never left without cover.

The decision to enforce a deadline on both sides came from a real concern: a substitute agreeing to cover a class and then cancelling the night before is just as disruptive as a teacher reporting absence on the day. Consistent rules on both sides make the system more reliable.


**Absence dates limited to teaching days**

When reporting an absence, teachers are only shown dates on which they actually have classes scheduled. This prevents confusion and invalid requests as there is no point reporting an absence for a Wednesday if the teacher has no Wednesday classes. The valid dates are generated on the backend by cross-referencing the teacher's assigned slots against the calendar, filtering to only days of the week where they teach, and presenting them as a clean dropdown.

This decision was debated as well. What if a main teacher swapped their class timing with another main teacher for a specific week, because more substitutes are available on that date, so reporting absence would be easier? Ultimately, it was decided that swapping specific classes should be a future feature instead, as to not compromise the reliability of the web app.


**Eligibility check before inserting the absence record**

An earlier version inserted the substitution row into the database first, then checked for eligible teachers. If no eligible teachers were found, the row was left orphaned in the database. This was corrected so the eligibility check runs before any database write. If no eligible teachers exist, the teacher sees an error and nothing is recorded. This keeps the database clean and avoids phantom pending requests that can never be fulfilled.


**Admin as a regular user**

Rather than a separate admin account type (which was considered), admins are regular teachers with an `is_admin` column set to TRUE in the database. This means they go through the same registration and login flow, appear in the teachers directory, can choose to teach classes, and can report absences and accept substitutions like anyone else. The `is_admin` field is set directly in the database, which is intentional, so only someone with database access can promote and demote an admin, keeping that action appropriately restricted.


**Teacher dropdown filtered by subject in timetable editor**

When an admin creates a new class time slot and chooses a subject (e.g., Mathematics), the teacher dropdown will show only teachers who can teach that subject. This is only partially to prevent accidental misassignments, as the primary reasoning behind it was to reduce the number of teachers in the dropdown for ease of creating the timetable. The filtering is done client-side using a subject-to-teacher map generated on the backend and passed to the template as JSON.

## Future plans

- Search and filter on the admin logs page
- Holiday blocking on the timetable
- Admin ability to manually assign a substitute if no one accepts
- Admin ability to manually cancel a substitute or absence request, regardless of the two day rule.
- Profile pictures for teachers
- Class swap feature - teacher A proposes swapping a class with teacher B for a specific date, teacher B accepts or declines
- Expansion toward a broader school management system: attendance, announcements, a parent-facing view, a student-facing view
- Colour coding and improved timetable visuals and filters
- Logs to show new users and their subjects

And muuuuuuuuuch later:
- Multi-school support with per-school data isolation, allowing the system to be offered as a hosted service to multiple schools
