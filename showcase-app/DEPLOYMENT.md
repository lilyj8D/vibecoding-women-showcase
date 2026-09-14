# Upgrade the existing deployment to Showcase Mystic

# Deployment Guide

Use the prepared package:

`dist/vibecoding-women-showcase.zip`

It contains the canonical `lambda_function.py` backend and `showcase.html` interface at the
archive root, matching the filenames used by the Lambda runtime.

## What stays unchanged

Do **not** recreate these resources:

- Lambda function
- API Gateway REST API and `prod` stage
- DynamoDB projects and votes tables
- Private S3 upload bucket
- Lambda execution role and inline policy
- Lambda environment variables
- SES identity and host notification settings

The new edit-token fields are ordinary DynamoDB attributes, so no table migration is needed.
The existing IAM policy already includes DynamoDB `GetItem`, `PutItem`, and `UpdateItem`, plus
S3 `PutObject`, which the edit flow needs.

## Deploy

1. AWS Console → Lambda → open `vibecoding-women-showcase`.
2. Confirm the region is `us-west-2`.
3. Open the **Code** tab.
4. Choose **Upload from → .zip file**.
5. Upload `dist/vibecoding-women-showcase.zip` from this repository.
6. Choose **Save** and wait until the function reports that the update succeeded.
7. Open your existing API Gateway `prod` Invoke URL and hard-refresh the page (`Ctrl+F5`).

## API Gateway

If the API was created using the documented `ANY /` and `ANY /{proxy+}` Lambda proxy routes,
no API Gateway change or redeployment is needed. The existing proxy automatically forwards the
`POST /update` and `POST /delete` paths.

If you created explicit routes instead, add both `POST /update` and `POST /delete` with Lambda
proxy integration to the same Lambda, then redeploy the `prod` stage.

## Smoke test

Use a new test submission because projects created before this release do not have edit tokens.

1. Submit a URL-based project.
2. Confirm the page displays a private edit key and lets you copy it.
3. Confirm that project's card shows **Edit** in the same browser.
4. Edit its title or description and save.
5. Confirm its vote count and creator name remain unchanged.
6. In a private/incognito window, choose **Edit with a saved key**, paste the key, and save a
   second change.
7. Try a deliberately modified key and confirm the update is rejected.
8. Delete the test project and confirm the warning dialog appears before removal.
9. Confirm the project disappears from the gallery and leaderboard after deletion.
10. If the project had a vote, confirm that voter can use the released vote on another project.
11. Verify normal voting, search, media, and Learn More still work.

## Important compatibility note

Only projects submitted after this ZIP is deployed receive an edit token. Earlier DynamoDB
project items remain visible and voteable, but cannot be self-edited because no secret token was
issued for them. A host can still correct an older item directly in DynamoDB if necessary.

## Security behavior

- The browser receives the raw token once after submission.
- DynamoDB stores only a SHA-256 hash of the token.
- Public `GET /projects` responses never contain token data.
- The token is also kept in that browser's localStorage for convenience.
- Editing and deletion close at the same deadline as submissions.
- Updates cannot change creator identity, original submission time, or vote count.
- Deletion is a recoverable soft delete: the project disappears from public routes immediately,
  its private S3 file can no longer be retrieved through the app, and votes for it stop consuming
  voters' allowances. The underlying DynamoDB record and private S3 object remain available to
  the host for recovery or audit, so no additional IAM delete permissions are required.
