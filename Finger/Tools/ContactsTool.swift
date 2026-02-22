import Contacts

enum ContactsTool {
    static func search(name: String) async -> String {
        let store = CNContactStore()
        do {
            try await store.requestAccess(for: .contacts)
        } catch {
            return "Error: Contacts access denied."
        }

        let keys: [CNKeyDescriptor] = [
            CNContactGivenNameKey as CNKeyDescriptor,
            CNContactFamilyNameKey as CNKeyDescriptor,
            CNContactPhoneNumbersKey as CNKeyDescriptor,
            CNContactEmailAddressesKey as CNKeyDescriptor,
        ]

        let request = CNContactFetchRequest(keysToFetch: keys)
        request.predicate = CNContact.predicateForContacts(matchingName: name)

        var results: [String] = []
        do {
            try store.enumerateContacts(with: request) { contact, _ in
                let phones = contact.phoneNumbers.map { $0.value.stringValue }.joined(separator: ", ")
                let emails = contact.emailAddresses.map { $0.value as String }.joined(separator: ", ")
                var line = "\(contact.givenName) \(contact.familyName)"
                if !phones.isEmpty { line += " — \(phones)" }
                if !emails.isEmpty { line += " — \(emails)" }
                results.append(line)
            }
        } catch {
            return "Error searching contacts: \(error.localizedDescription)"
        }

        return results.isEmpty ? "No contacts found for '\(name)'." : results.joined(separator: "\n")
    }
}
