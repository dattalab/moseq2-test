# Security policy

Report vulnerabilities through the repository's private GitHub security
advisory interface. Do not place credentials, signed URLs, private O2 paths, or
restricted datasets in issues or run reports.

Public pull-request code runs only on disposable GitHub-hosted workers with
`contents: read` and no secrets. It is never run by `pull_request_target` or on
an O2/self-hosted runner. Pickles are deserialized only by the disposable
target worker after an immutable manifest marks them as trusted baseline data.

