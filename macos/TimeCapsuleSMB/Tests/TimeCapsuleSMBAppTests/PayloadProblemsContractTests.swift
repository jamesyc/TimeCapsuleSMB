import XCTest
@testable import TimeCapsuleSMBApp

/// DoctorSummary.payloadProblems mirrors the helper's table of manager
/// verdicts: `payload_problems.json`, which `tests/fixtures/payload_problems.py`
/// generates from device.probe.DISK_PAYLOAD_PROBLEMS. The same codes in the same
/// order, with the same answer to whether Samba is off for each and whether a
/// new disk is what fixes it.
final class PayloadProblemsContractTests: XCTestCase {
    private struct Verdict: Decodable {
        let code: String
        let sambaOff: Bool
        let replacesDisk: Bool

        enum CodingKeys: String, CodingKey {
            case code
            case sambaOff = "samba_off"
            case replacesDisk = "replaces_disk"
        }
    }

    func testTheAppKnowsEveryVerdictTheHelperReports() throws {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "payload_problems", withExtension: "json", subdirectory: "Fixtures"))
        let verdicts = try JSONDecoder().decode([Verdict].self, from: Data(contentsOf: url))
        XCTAssertEqual(DoctorSummary.payloadProblems.map(\.code), verdicts.map(\.code))
        XCTAssertEqual(DoctorSummary.payloadProblems.map(\.stopsSamba), verdicts.map(\.sambaOff))
        XCTAssertEqual(DoctorSummary.payloadProblems.map(\.replacesDisk), verdicts.map(\.replacesDisk))
    }
}
