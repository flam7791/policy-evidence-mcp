# About this sample corpus

These documents belong to the Aurora Institute, a **fictional** policy organisation invented for
this repository. They exist so that the retrieval pipeline, the sensitivity ceiling and the
evaluation set can be tested end to end without using any real organisation's material.

Folders mirror sensitivity levels: files under `public/`, `internal/` and `restricted/` are
classified accordingly unless their front matter says otherwise. With the default ceiling
(public) only public files are indexed; the evaluation runs with the ceiling set to internal and
checks that restricted files are never returned.

README files are not indexed.
