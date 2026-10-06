import { LightningElement, wire } from 'lwc';
import { refreshApex } from '@salesforce/apex';
import submitRegistration from '@salesforce/apex/EventRegistrationController.submitRegistration';
import updateRegistration from '@salesforce/apex/EventRegistrationController.updateRegistration';
import getRecentRegistrations from '@salesforce/apex/EventRegistrationController.getRecentRegistrations';

export default class EventRegistrationForm extends LightningElement {
    attendeeName = '';
    email = '';
    phone = '';
    numberOfGuests;
    eventDate;

    isSubmitting = false;
    showSuccess = false;
    errorMessage = '';
    successMessage = '';
    isEditing = false;
    editingRegistrationId;

    registrations;

    get submitButtonLabel() {
        return this.isEditing ? 'Save Changes' : 'Submit Registration';
    }

    @wire(getRecentRegistrations)
    wiredRegistrations(result) {
        this.registrations = result;
    }

    handleNameChange(event) {
        this.attendeeName = event.target.value;
    }

    handleEmailChange(event) {
        this.email = event.target.value;
    }

    handlePhoneChange(event) {
        this.phone = event.target.value;
    }

    handleGuestsChange(event) {
        const rawValue = event.target.value;
        this.numberOfGuests = rawValue === '' || rawValue === null || rawValue === undefined
            ? undefined
            : parseInt(rawValue, 10);
    }

    handleDateChange(event) {
        this.eventDate = event.target.value;
    }

    handleEdit(event) {
        const registration = this.registrations?.data?.find(
            (item) => item.Id === event.currentTarget.dataset.registrationId
        );
        if (!registration) {
            this.errorMessage = 'The selected registration could not be loaded.';
            this.showSuccess = false;
            return;
        }

        Object.assign(this, {
            attendeeName: registration.Attendee_Name__c,
            email: registration.Email__c,
            phone: registration.Phone__c || '',
            numberOfGuests: registration.Number_of_Guests__c,
            eventDate: registration.Event_Date__c,
            editingRegistrationId: registration.Id,
            isEditing: true,
            showSuccess: false,
            errorMessage: ''
        });
    }

    async handleSubmit() {
        this.showSuccess = false;
        this.errorMessage = '';

        if (!this.isEditing) {
            const emailPattern = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
            if (this.email && !emailPattern.test(this.email.trim())) {
                this.errorMessage = 'A valid email address is required.';
                return;
            }
        }

        this.isSubmitting = true;

        try {
            const result = this.isEditing
                ? await updateRegistration({
                    registrationId: this.editingRegistrationId,
                    phone: this.phone,
                    numberOfGuests: this.numberOfGuests,
                    eventDate: this.eventDate
                })
                : await submitRegistration({
                    attendeeName: this.attendeeName,
                    email: this.email,
                    phone: this.phone,
                    numberOfGuests: this.numberOfGuests,
                    eventDate: this.eventDate
                });
            this.successMessage = this.isEditing
                ? 'Registration updated successfully.'
                : `Registration submitted successfully! Confirmation number: ${result}`;
            this.showSuccess = true;
            this.isEditing = false;
            this.editingRegistrationId = undefined;
            this.resetForm();
            await refreshApex(this.registrations);
        } catch (error) {
            this.showSuccess = false;
            this.errorMessage = this.extractErrorMessage(error);
        } finally {
            this.isSubmitting = false;
        }
    }

    resetForm() {
        this.attendeeName = '';
        this.email = '';
        this.phone = '';
        this.numberOfGuests = undefined;
        this.eventDate = undefined;
        this.template.querySelectorAll('lightning-input').forEach((input) => {
            input.value = '';
        });
    }

    extractErrorMessage(error) {
        if (error && error.body && error.body.message) {
            return error.body.message;
        }
        if (error && error.message) {
            return error.message;
        }
        return 'An unexpected error occurred while submitting the registration.';
    }
}
