@isTest
private class EventRegistrationControllerTest {

    @isTest
    static void testSuccessfulRegistration() {
        Test.startTest();
        Id newId = EventRegistrationController.submitRegistration(
            'Jane Doe', 'jane.doe@example.com', '555-123-4567', 4, Date.today().addDays(10)
        );
        Test.stopTest();

        System.assertNotEquals(null, newId, 'A new registration record should have been created.');
        Event_Registration__c reg = [SELECT Attendee_Name__c, Email__c, Status__c, Number_of_Guests__c
                                      FROM Event_Registration__c WHERE Id = :newId];
        System.assertEquals('Jane Doe', reg.Attendee_Name__c);
        System.assertEquals('jane.doe@example.com', reg.Email__c);
        System.assertEquals('Pending', reg.Status__c);
        System.assertEquals(4, reg.Number_of_Guests__c);
    }

    @isTest
    static void testBlankNameIsRejected() {
        Boolean threw = false;
        try {
            EventRegistrationController.submitRegistration(
                '', 'jane.doe@example.com', null, 2, Date.today().addDays(5)
            );
        } catch (AuraHandledException e) {
            threw = true;
        }
        System.assert(threw, 'Blank attendee name should be rejected.');
    }

    @isTest
    static void testInvalidEmailIsRejected() {
        String errorMessage;
        try {
            EventRegistrationController.submitRegistration(
                'John Smith', 'notanemail', null, 2, Date.today().addDays(5)
            );
        } catch (AuraHandledException e) {
            errorMessage = e.getMessage();
        }
        System.assertEquals('A valid email address is required.', errorMessage,
            'CLAUDE-15: Invalid email format should be rejected with a clear message.');
    }

    @isTest
    static void testZeroGuestsIsRejected() {
        String errorMessage;
        try {
            EventRegistrationController.submitRegistration(
                'John Smith', 'john@example.com', null, 0, Date.today().addDays(5)
            );
        } catch (AuraHandledException e) {
            errorMessage = e.getMessage();
        }
        System.assertEquals('Number of guests must be between 1 and 10.', errorMessage,
            'CLAUDE-16: Zero guests should be rejected with the guest-count validation message.');
    }

    @isTest
    static void testNegativeGuestsIsRejected() {
        String errorMessage;
        try {
            EventRegistrationController.submitRegistration(
                'John Smith', 'john@example.com', null, -3, Date.today().addDays(5)
            );
        } catch (AuraHandledException e) {
            errorMessage = e.getMessage();
        }
        System.assertEquals('Number of guests must be between 1 and 10.', errorMessage,
            'CLAUDE-16: Negative guests should be rejected with the guest-count validation message.');
    }

    @isTest
    static void testMinimumBoundaryGuestsIsAccepted() {
        Test.startTest();
        Id newId = EventRegistrationController.submitRegistration(
            'Boundary Tester', 'minimum@example.com', null, 1, Date.today().addDays(5)
        );
        Test.stopTest();

        System.assertNotEquals(null, newId, 'CLAUDE-16: Exactly 1 guest (the minimum boundary) should be accepted.');
    }

    @isTest
    static void testElevenGuestsIsRejected() {
        String errorMessage;
        try {
            EventRegistrationController.submitRegistration(
                'John Smith', 'john@example.com', null, 11, Date.today().addDays(5)
            );
        } catch (AuraHandledException e) {
            errorMessage = e.getMessage();
        }
        System.assertEquals('Number of guests must be between 1 and 10.', errorMessage,
            'CLAUDE-19: Eleven guests should be rejected with the guest-count validation message.');
    }

    @isTest
    static void testMaxBoundaryGuestsIsAccepted() {
        Test.startTest();
        Id newId = EventRegistrationController.submitRegistration(
            'Boundary Tester', 'boundary@example.com', null, 10, Date.today().addDays(5)
        );
        Test.stopTest();
        System.assertNotEquals(null, newId, 'Exactly 10 guests (the max boundary) should be accepted.');
    }

    @isTest
    static void testPastEventDateIsRejected() {
        String errorMessage;
        try {
            EventRegistrationController.submitRegistration(
                'John Smith', 'john@example.com', null, 2, Date.today().addDays(-1)
            );
        } catch (AuraHandledException e) {
            errorMessage = e.getMessage();
        }
        System.assertEquals('Event date must be in the future.', errorMessage,
            'CLAUDE-17: A past event date should be rejected with the expected message.');
    }

    @isTest
    static void testTodayEventDateIsRejected() {
        String errorMessage;
        try {
            EventRegistrationController.submitRegistration(
                'John Smith', 'john@example.com', null, 2, Date.today()
            );
        } catch (AuraHandledException e) {
            errorMessage = e.getMessage();
        }
        System.assertEquals('Event date must be in the future.', errorMessage,
            'CLAUDE-17: A same-day event date should be rejected with the expected message.');
    }

    @isTest
    static void testGetRecentRegistrationsReturnsNewestFirst() {
        EventRegistrationController.submitRegistration(
            'First Person', 'first@example.com', null, 2, Date.today().addDays(5)
        );
        EventRegistrationController.submitRegistration(
            'Second Person', 'second@example.com', null, 3, Date.today().addDays(6)
        );

        Test.startTest();
        List<Event_Registration__c> results = EventRegistrationController.getRecentRegistrations();
        Test.stopTest();

        System.assertEquals(2, results.size(), 'Both registrations should be returned.');
        System.assertEquals('Second Person', results[0].Attendee_Name__c,
            'Most recently created registration should be first.');
    }

    @isTest
    static void testUpdateRegistrationAndValidation() {
        Id registrationId = createRegistrationForUpdate(); Date updatedEventDate = Date.today().addDays(12);

        Test.startTest();
        EventRegistrationController.updateRegistration(registrationId, '555-987-6543', 10, updatedEventDate);
        for (Integer guests : new List<Integer>{ null, 0, 11 }) {
            assertUpdateRejected(registrationId, guests, Date.today().addDays(5), 'Number of guests must be between 1 and 10.');
        }
        for (Date invalidDate : new List<Date>{ null, Date.today(), Date.today().addDays(-1) }) {
            assertUpdateRejected(registrationId, 2, invalidDate, 'Event date must be in the future.');
        }
        assertUpdateRejected(null, 2, Date.today().addDays(5), 'Registration was not found or is not accessible.');
        Test.stopTest();

        Event_Registration__c registration = [SELECT Attendee_Name__c, Email__c, Phone__c,
            Number_of_Guests__c, Event_Date__c FROM Event_Registration__c
            WHERE Id = :registrationId];
        System.assertEquals('Edit Tester', registration.Attendee_Name__c, 'CLAUDE-18: Name is unchanged.');
        System.assertEquals('edit.tester@example.com', registration.Email__c, 'CLAUDE-18: Email is unchanged.');
        System.assertEquals('555-987-6543', registration.Phone__c);
        System.assertEquals(10, registration.Number_of_Guests__c, 'CLAUDE-18: Max guests should be accepted.');
        System.assertEquals(updatedEventDate, registration.Event_Date__c);
    }

    private static Id createRegistrationForUpdate() {
        return EventRegistrationController.submitRegistration('Edit Tester', 'edit.tester@example.com',
            '555-111-2222', 2, Date.today().addDays(5));
    }

    private static void assertUpdateRejected(Id registrationId, Integer guests,
                                             Date eventDate, String expectedMessage) {
        String errorMessage;
        try { EventRegistrationController.updateRegistration(registrationId, '555-987-6543', guests, eventDate); }
        catch (AuraHandledException e) { errorMessage = e.getMessage(); }
        System.assertEquals(expectedMessage, errorMessage,
            'CLAUDE-18: Invalid update values must be rejected with the expected message.');
    }
}
