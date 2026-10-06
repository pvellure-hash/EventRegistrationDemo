import { createElement } from 'lwc';
import { registerApexTestWireAdapter } from '@salesforce/sfdx-lwc-jest';
import getRecentRegistrations from '@salesforce/apex/EventRegistrationController.getRecentRegistrations';
import EventRegistrationForm from 'c/eventRegistrationForm';

const getRecentRegistrationsAdapter = registerApexTestWireAdapter(getRecentRegistrations);

it('prefills editable details and keeps attendee name and email read-only', async () => {
    const element = createElement('c-event-registration-form', { is: EventRegistrationForm });
    document.body.appendChild(element);
    getRecentRegistrationsAdapter.emit([{
        Id: 'registration-test-id',
        Attendee_Name__c: 'Edit Tester',
        Email__c: 'edit.tester@example.com',
        Phone__c: '555-111-2222',
        Number_of_Guests__c: 2,
        Event_Date__c: '2027-01-15',
    }]);
    await Promise.resolve();
    element.shadowRoot.querySelector('lightning-button[data-registration-id="registration-test-id"]').click();
    await Promise.resolve();

    const input = (label) => Array.from(element.shadowRoot.querySelectorAll('lightning-input')).find((item) => item.label === label);
    expect(input('Attendee Name').value).toBe('Edit Tester');
    expect(input('Attendee Name').disabled).toBe(true);
    expect(input('Email').value).toBe('edit.tester@example.com');
    expect(input('Email').disabled).toBe(true);
    expect(input('Phone (optional)').value).toBe('555-111-2222');
    expect(input('Number of Guests (1-10)').value).toBe('2');
    expect(input('Event Date').value).toBe('2027-01-15');
    expect(element.shadowRoot.querySelector('lightning-button[label="Save Changes"]')).not.toBeNull();
});
