# Import processing decisions

The original catalog importer used a scheduled polling job. This was accepted
because the first integration partner only published a nightly export.

The polling design now delays inventory updates and repeatedly scans unchanged
rows. The team proposes replacing it with event-driven import workers. The new
workers depend on the existing system baseline and supersede scheduled polling.

