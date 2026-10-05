/**
 * EventRegistrationController
 *
 * Handles submission and retrieval of Event Registration records for the
 * eventRegistrationForm Lightning Web Component.
 *
 * Validation rules enforced server-side (never trust client-side validation
 * alone):
 *   - Attendee name is required.
 *   - Email is required and must match a valid email format.
 *   - Number of guests is required and must be between 1 and 10 inclusive.
 *   - Event date is required and must be strictly in the future (today does
 *     not count, since same-day registration is not supported by this
 *     process).
 */
public with sharing class EventRegistrationController {

    private static final Integer MIN_GUESTS = 1;
    private static final Integer MAX_GUESTS = 10;
    private static final String EMAIL_REGEX = '^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}$';

    @AuraEnabled
    public static Id submitRegistration(String attendeeName, String email, String phone,
                                         Integer numberOfGuests, Date eventDate) {
        validateInputs(attendeeName, email, numberOfGuests, eventDate);

        if (!Schema.sObjectType.Event_Registration__c.isCreateable()) {
            throw new AuraHandledException('You do not have permission to create registrations.');
        }

        Event_Registration__c reg = new Event_Registration__c(
            Attendee_Name__c = attendeeName.trim(),
            Email__c = email.trim(),
            Phone__c = phone,
            Number_of_Guests__c = numberOfGuests,
            Event_Date__c = eventDate,
            Status__c = 'Pending'
        );

        insert reg;
        return reg.Id;
    }

    @AuraEnabled(cacheable=true)
    public static List<Event_Registration__c> getRecentRegistrations() {
        if (!Schema.sObjectType.Event_Registration__c.isAccessible()) {
            return new List<Event_Registration__c>();
        }
        return [
            SELECT Id, Name, Attendee_Name__c, Email__c, Phone__c,
                   Number_of_Guests__c, Event_Date__c, Status__c, CreatedDate
            FROM Event_Registration__c
            WITH SECURITY_ENFORCED
            ORDER BY CreatedDate DESC, Id DESC
            LIMIT 10
        ];
    }

    private static void validateInputs(String attendeeName, String email,
                                        Integer numberOfGuests, Date eventDate) {
        List<String> errors = new List<String>();

        if (String.isBlank(attendeeName)) {
            errors.add('Attendee name is required.');
        }

        if (String.isBlank(email) || !isValidEmail(email.trim())) {
            errors.add('A valid email address is required.');
        }

        if (numberOfGuests == null || numberOfGuests < MIN_GUESTS || numberOfGuests > MAX_GUESTS) {
            errors.add('Number of guests must be between ' + MIN_GUESTS + ' and ' + MAX_GUESTS + '.');
        }

        if (eventDate == null || eventDate <= Date.today()) {
            errors.add('Event date must be in the future.');
        }

        if (!errors.isEmpty()) {
			AuraHandledException ex = new AuraHandledException(String.join(errors, ' '));
			ex.setMessage(String.join(errors, ' '));
			throw ex;
		}
    }

    private static Boolean isValidEmail(String email) {
        Pattern p = Pattern.compile(EMAIL_REGEX);
        Matcher m = p.matcher(email);
        return m.matches();
    }
}
