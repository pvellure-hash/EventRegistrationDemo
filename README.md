# Event Registration App

A small, self-contained Salesforce application built specifically to test
an end-to-end Jira → GitHub Copilot → GitHub → Salesforce automation
pipeline on fresh, purpose-built code (not a pre-existing project).

## What it does

A public-facing style registration form (Lightning Web Component) where a
user enters:
- Attendee Name
- Email
- Phone (optional)
- Number of Guests (1-10)
- Event Date (must be in the future)

On submit, `EventRegistrationController.submitRegistration()` validates all
inputs server-side and, if valid, creates an `Event_Registration__c` record
with `Status__c = 'Pending'`. The same page shows the 10 most recent
registrations in a table underneath the form.

## Components

| File | Purpose |
|---|---|
| `force-app/main/default/objects/Event_Registration__c/` | Custom object + 6 custom fields |
| `force-app/main/default/classes/EventRegistrationController.cls` | Apex controller: validation + record creation + query |
| `force-app/main/default/classes/EventRegistrationControllerTest.cls` | 10 test methods covering every validation rule, including boundary cases |
| `force-app/main/default/lwc/eventRegistrationForm/` | The Lightning Web Component form + results table |
| `force-app/main/default/tabs/Event_Registration__c.tab-meta.xml` | Custom Tab so the object shows in App Launcher |
| `force-app/main/default/permissionsets/Event_Registration_User.permissionset-meta.xml` | Grants object/field/class access to assigned users |
| `.github/workflows/salesforce-validate.yml` | CI check on every PR: dry-run deploy + run local tests |
| `.github/workflows/salesforce-deploy.yml` | CI on merge to `main`: real deploy + run local tests |

See the accompanying Word document for full step-by-step setup,
deployment, testing, and agent-pipeline integration instructions.
