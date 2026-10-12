import XCTest
@testable import TimeCapsuleSMBApp

/// Apple's conflict renames of a service name, checked against answers from
/// Apple's own mDNSCore code in the firmware (Fixtures/apple_mdns_names.json,
/// which the helper's tests read too).
final class AppleBonjourNameTests: XCTestCase {
    private struct Fixture: Decodable {
        struct Rename: Decodable {
            let name: String
            let serviceName: Bool
            let renamed: String

            enum CodingKeys: String, CodingKey {
                case name, renamed
                case serviceName = "service_name"
            }
        }

        let renames: [Rename]
    }

    private func serviceRenames() throws -> [Fixture.Rename] {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "apple_mdns_names", withExtension: "json", subdirectory: "Fixtures"))
        let renames = try JSONDecoder().decode(Fixture.self, from: Data(contentsOf: url)).renames.filter(\.serviceName)
        XCTAssertFalse(renames.isEmpty)
        return renames
    }

    private func number(in renamed: String) throws -> Int {
        try XCTUnwrap(Int(try XCTUnwrap(renamed.split(separator: "(").last).dropLast()))
    }

    func testAConflictRenamesAServiceNameAsApplesFirmwareDoes() throws {
        for rename in try serviceRenames() {
            XCTAssertEqual(AppleBonjourName.conflictName(rename.name, number: try number(in: rename.renamed)), rename.renamed, rename.name)
            XCTAssertTrue(AppleBonjourName.isConflictRename(rename.renamed, of: rename.name), rename.name)
            XCTAssertTrue(AppleBonjourName.hasConflictSuffix(rename.renamed), rename.renamed)
        }
    }

    func testOtherNamesAreNotApplesRename() {
        let cases: [(String, String)] = [
            ("Capsule  (2)", "Capsule "),  // Apple drops the trailing space first
            ("Capsule (1)", "Capsule"),    // the first rename is 2
            ("Capsule (2)", "Capsule (3)"),  // the number never goes down
            ("capsule (2)", "Capsule"),    // a service name keeps its case
            ("Capsule 2", "Capsule"),
            ("Capsule(2)", "Capsule"),
            ("Office (2)", "Capsule"),
        ]
        for (advertised, original) in cases {
            XCTAssertFalse(AppleBonjourName.isConflictRename(advertised, of: original), "\(advertised) / \(original)")
        }
        // Apple's numbers start at 2: a 1 or a 0 is part of the name.
        for name in ["Capsule", "Capsule 2", "Capsule(2)", "Capsule (x)", "(2)", "Capsule (1)", "Capsule (0)"] {
            XCTAssertFalse(AppleBonjourName.hasConflictSuffix(name), name)
        }
    }

    func testNamesCompareAsBonjourComparesThem() {
        // SameDomainLabel and RFC 6762 section 16: ASCII case folds, nothing else.
        XCTAssertEqual(AppleBonjourName.key("AirPort Time Capsule"), AppleBonjourName.key("airport TIME capsule"))
        XCTAssertNotEqual(AppleBonjourName.key("Été"), AppleBonjourName.key("été"))
        XCTAssertNotEqual(AppleBonjourName.key("Жук"), AppleBonjourName.key("жук"))
        // Swift's == finds these equal; Bonjour does not.
        let composed = "Caf\u{E9}", decomposed = "Cafe\u{301}"
        XCTAssertEqual(composed, decomposed)
        XCTAssertNotEqual(AppleBonjourName.key(composed), AppleBonjourName.key(decomposed))
    }

    func testTheBaseIsWhatTwoDevicesCollideOn() {
        XCTAssertEqual(AppleBonjourName.conflictBase("AirPort Time Capsule (2)"), "AirPort Time Capsule")
        XCTAssertEqual(AppleBonjourName.conflictBase("Capsule "), "Capsule")
        XCTAssertEqual(AppleBonjourName.conflictBase(" Capsule"), " Capsule")
        XCTAssertEqual(AppleBonjourName.conflictBase("Office 2"), "Office 2")
        XCTAssertEqual(AppleBonjourName.conflictBase("Office (1)"), "Office (1)")
    }
}
