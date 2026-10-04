import { LightningElement, wire } from 'lwc';
import { refreshApex } from '@salesforce/apex';
import submitRegistration from '@salesforce/apex/EventRegistrationController.submitRegistration';
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
    lastRegistrationId = '';

    registrations;

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
        this.numberOfGuests = parseInt(event.target.value, 10);
    }

    handleDateChange(event) {
        this.eventDate = event.target.value;
    }

    async handleSubmit() {
        this.showSuccess = false;
        this.errorMessage = '';
        this.isSubmitting = true;

        try {
            const newId = await submitRegistration({
                attendeeName: this.attendeeName,
                email: this.email,
                phone: this.phone,
                numberOfGuests: this.numberOfGuests,
                eventDate: this.eventDate
            });

            this.lastRegistrationId = newId;
            this.showSuccess = true;
            this.resetForm();
            await refreshApex(this.registrations);
        } catch (error) {
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
