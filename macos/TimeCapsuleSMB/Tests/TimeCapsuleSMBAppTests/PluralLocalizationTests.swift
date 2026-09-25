import XCTest
@testable import TimeCapsuleSMBApp

/// Plural sentences come from Localizable.stringsdict. Foundation chooses the
/// form from the locale the app formats with, so these tests render through
/// the app's own paths (BackendSummary and L10n.format) in every language.
/// tests/test_localization_plurals.py checks the files' structure.
final class PluralLocalizationTests: XCTestCase {
    private var originalLanguage: AppLanguage = .system

    override func setUp() {
        super.setUp()
        originalLanguage = L10n.currentLanguage
    }

    override func tearDown() {
        L10n.apply(language: originalLanguage)
        super.tearDown()
    }

    private static let languages = AppLanguage.allCases.filter { $0 != .system }

    /// Counts at every boundary between CLDR categories in the ten languages.
    private static let edgeCounts = [0, 1, 2, 3, 4, 5, 9, 10, 11, 12, 14, 19, 20, 21, 22, 25, 100, 101, 111, 112, 1_000, 1_000_000, 2_000_000]

    /// The CLDR category of a non-negative integer count; the same rule as
    /// `plural_rule` in tests/test_localization_plurals.py.
    private static func category(_ language: AppLanguage, _ count: Int) -> String {
        let last = count % 10
        let lastTwo = count % 100
        switch language {
        case .english, .german, .dutch:
            return count == 1 ? "one" : "other"
        case .spanish, .italian:
            if count == 1 { return "one" }
            return count != 0 && count % 1_000_000 == 0 ? "many" : "other"
        case .french, .portuguese:
            if count == 0 || count == 1 { return "one" }
            return count % 1_000_000 == 0 ? "many" : "other"
        case .russian:
            if last == 1 && lastTwo != 11 { return "one" }
            if (2...4).contains(last) && !(12...14).contains(lastTwo) { return "few" }
            return "many"
        case .lithuanian:
            if last == 1 && !(11...19).contains(lastTwo) { return "one" }
            if (2...9).contains(last) && !(11...19).contains(lastTwo) { return "few" }
            return "other"
        case .simplifiedChinese, .system:
            return "other"
        }
    }

    private func pluralEntries(_ language: AppLanguage) throws -> [String: [String: Any]] {
        let identifier = try XCTUnwrap(language.localizationIdentifier)
        let url = try XCTUnwrap(
            AppResourceBundle.bundle.url(forResource: "Localizable", withExtension: "stringsdict", subdirectory: nil, localization: identifier),
            "\(identifier) has no Localizable.stringsdict"
        )
        let plist = try PropertyListSerialization.propertyList(from: Data(contentsOf: url), format: nil)
        return try XCTUnwrap(plist as? [String: [String: Any]])
    }

    private func render(_ key: String, _ arguments: [BackendSummaryArgument], in language: AppLanguage) -> String? {
        BackendSummary(key: key, arguments: arguments, text: "unused").resolved(language: language)
    }

    // MARK: Exact sentences

    func testCountSummariesUseTheGrammaticalFormForEachCount() {
        let cases: [(AppLanguage, String, [BackendSummaryArgument], String)] = [
            (.english, "backend.summary.discovered_devices", [.int(0)], "Discovered 0 devices."),
            (.english, "backend.summary.discovered_devices", [.int(1)], "Discovered 1 device."),
            (.english, "backend.summary.discovered_devices", [.int(2)], "Discovered 2 devices."),
            (.german, "backend.summary.discovered_devices", [.int(1)], "1 Gerät gefunden."),
            (.german, "backend.summary.discovered_devices", [.int(2)], "2 Geräte gefunden."),
            (.french, "backend.summary.discovered_devices", [.int(0)], "0 appareil découvert."),
            (.french, "backend.summary.discovered_devices", [.int(2)], "2 appareils découverts."),
            (.spanish, "backend.summary.discovered_devices", [.int(1)], "Se descubrió 1 dispositivo."),
            (.spanish, "backend.summary.discovered_devices", [.int(3)], "Se descubrieron 3 dispositivos."),
            (.portuguese, "backend.summary.discovered_devices", [.int(0)], "0 dispositivos encontrados."),
            (.portuguese, "backend.summary.discovered_devices", [.int(1)], "1 dispositivo encontrado."),
            (.portuguese, "backend.summary.discovered_devices", [.int(2)], "2 dispositivos encontrados."),
            (.portuguese, "backend.summary.repair_xattrs_found", [.int(0), .int(0)],
             "Foram encontrados 0 problemas de metadados; 0 são reparáveis."),
            (.portuguese, "backend.summary.repair_xattrs_found", [.int(1), .int(1)],
             "Foi encontrado 1 problema de metadados; 1 é reparável."),
            (.russian, "backend.summary.discovered_devices", [.int(1)], "Обнаружено 1 устройство."),
            (.russian, "backend.summary.discovered_devices", [.int(3)], "Обнаружено 3 устройства."),
            (.russian, "backend.summary.discovered_devices", [.int(11)], "Обнаружено 11 устройств."),
            (.russian, "backend.summary.discovered_devices", [.int(21)], "Обнаружено 21 устройство."),
            (.lithuanian, "backend.summary.discovered_devices", [.int(1)], "Rastas 1 įrenginys."),
            (.lithuanian, "backend.summary.discovered_devices", [.int(5)], "Rasti 5 įrenginiai."),
            (.lithuanian, "backend.summary.discovered_devices", [.int(10)], "Rasta 10 įrenginių."),
            (.lithuanian, "backend.summary.discovered_devices", [.int(21)], "Rastas 21 įrenginys."),
            (.simplifiedChinese, "backend.summary.discovered_devices", [.int(1)], "发现 1 个设备。"),
            (.english, "backend.summary.hfs_volumes_found", [.int(1)], "Found 1 mounted HFS volume."),
            (.russian, "backend.summary.hfs_volumes_found", [.int(1)], "Найден 1 смонтированный том HFS."),
            (.russian, "backend.summary.hfs_volumes_found", [.int(2)], "Найдено 2 смонтированных тома HFS."),
            (.russian, "backend.summary.hfs_volumes_found", [.int(5)], "Найдено 5 смонтированных томов HFS."),
            (.english, "backend.summary.repair_xattrs_found", [.int(1), .int(0)], "Found 1 metadata issue, 0 repairable."),
            (.english, "backend.summary.repair_xattrs_found", [.int(3), .int(1)], "Found 3 metadata issues, 1 repairable."),
            (.french, "backend.summary.repair_xattrs_found", [.int(1), .int(1)],
             "1 problème de métadonnées trouvé, dont 1 réparable."),
            (.french, "backend.summary.repair_xattrs_found", [.int(3), .int(2)],
             "3 problèmes de métadonnées trouvés, dont 2 réparables."),
            (.russian, "backend.summary.repair_xattrs_found", [.int(1), .int(1)],
             "Найдена 1 проблема с метаданными, из них 1 исправимая."),
            (.russian, "backend.summary.repair_xattrs_found", [.int(22), .int(5)],
             "Найдено 22 проблемы с метаданными, из них 5 исправимых."),
            (.lithuanian, "backend.summary.repair_xattrs_found", [.int(2), .int(0)],
             "Rastos 2 metaduomenų problemos, iš jų 0 pataisomų."),
            (.english, "backend.summary.flash.apple_some_match", [.int(1), .int(2)],
             "1 of 2 candidate firmware banks matches Apple stock firmware."),
            (.english, "backend.summary.flash.apple_some_match_version", [.int(2), .int(3), .string("7.8.1")],
             "2 of 3 candidate firmware banks match Apple stock firmware 7.8.1."),
            (.german, "backend.summary.flash.apple_some_match", [.int(1), .int(2)],
             "1 von 2 infrage kommenden Firmware-Bänken entspricht der Apple-Originalfirmware."),
            (.russian, "backend.summary.flash.apple_some_match", [.int(1), .int(21)],
             "1 из 21 проверяемого банка прошивки соответствует оригинальной прошивке Apple."),
            (.russian, "backend.summary.flash.apple_some_match_version", [.int(2), .int(5), .string("7.8.1")],
             "2 из 5 проверяемых банков прошивки соответствуют оригинальной прошивке Apple 7.8.1."),
            (.lithuanian, "backend.summary.flash.apple_some_match", [.int(1), .int(21)],
             "1 iš 21 tikrinamo programinės įrangos banko atitinka Apple originalią programinę įrangą."),
            (.simplifiedChinese, "backend.summary.flash.apple_some_match_version", [.int(1), .int(2), .string("7.8.1")],
             "2 个候选固件区中有 1 个与苹果原厂固件 7.8.1 匹配。"),
            (.english, "backend.summary.repair_xattrs_no_safe_repairs", [.int(1)],
             "Found 1 metadata issue, but no known-safe repair is available."),
            (.russian, "backend.summary.repair_xattrs_no_safe_repairs", [.int(5)],
             "Найдено 5 проблем с метаданными, но известного безопасного исправления нет."),
            (.english, "backend.summary.repair_xattrs_unresolved", [.int(1)], "1 metadata issue remains after repair."),
            (.english, "backend.summary.repair_xattrs_unresolved", [.int(2)], "2 metadata issues remain after repair."),
            (.german, "backend.summary.repair_xattrs_unresolved", [.int(1)], "Nach der Reparatur bleibt 1 Metadatenproblem bestehen."),
            (.russian, "backend.summary.repair_xattrs_unresolved", [.int(21)], "После исправления осталась 21 проблема с метаданными."),
            (.lithuanian, "backend.summary.repair_xattrs_unresolved", [.int(12)], "Po pataisymo liko 12 metaduomenų problemų.")
        ]

        for (language, key, arguments, expected) in cases {
            XCTAssertEqual(render(key, arguments, in: language), expected, "\(language.rawValue) \(key) \(arguments)")
        }
    }

    func testMillionsUseTheManyFormWhereTheLanguageHasOne() {
        let french = render("backend.summary.discovered_devices", [.int(1_000_000)], in: .french)
        XCTAssertTrue(french?.hasSuffix(" d’appareils découverts.") == true, french ?? "nil")
        let spanish = render("backend.summary.discovered_devices", [.int(2_000_000)], in: .spanish)
        XCTAssertTrue(spanish?.hasSuffix(" de dispositivos.") == true, spanish ?? "nil")
        let english = render("backend.summary.discovered_devices", [.int(1_000_000)], in: .english)
        XCTAssertTrue(english?.hasSuffix(" devices.") == true, english ?? "nil")
    }

    func testActivityCountFollowsTheAppLanguage() {
        let cases: [(AppLanguage, Int, String)] = [
            (.english, 2, "2 active operations"),
            (.german, 2, "2 aktive Vorgänge"),
            (.russian, 2, "2 активные операции"),
            (.russian, 5, "5 активных операций"),
            (.russian, 21, "21 активная операция"),
            (.lithuanian, 2, "2 aktyvios operacijos"),
            (.lithuanian, 10, "10 aktyvių operacijų"),
            (.lithuanian, 21, "21 aktyvi operacija"),
            (.simplifiedChinese, 3, "3 个正在进行的操作")
        ]
        for (language, count, expected) in cases {
            L10n.apply(language: language)
            XCTAssertEqual(L10n.format("activity.multiple_active", count), expected, "\(language.rawValue) \(count)")
        }
    }

    func testMissingArtifactsMessageAgreesWithTheCount() {
        func message(_ count: Int) -> String {
            BundleRuntimeIssue(code: .distributionArtifactsMissing, severity: .error, context: String(count)).message
        }

        L10n.apply(language: .english)
        XCTAssertEqual(message(1), "The bundled TimeCapsuleSMB distribution is missing 1 payload artifact.")
        XCTAssertEqual(message(3), "The bundled TimeCapsuleSMB distribution is missing 3 payload artifacts.")
        L10n.apply(language: .german)
        XCTAssertEqual(message(1), "Im mitgelieferten TimeCapsuleSMB-Paket fehlt 1 Installationsdatei.")
        L10n.apply(language: .lithuanian)
        XCTAssertEqual(message(21), "Su programa pateiktame TimeCapsuleSMB platinime trūksta 21 diegimo failo.")
        XCTAssertEqual(message(12), "Su programa pateiktame TimeCapsuleSMB platinime trūksta 12 diegimo failų.")
    }

    // MARK: Every key, language and boundary count

    func testEveryPluralKeyRendersTheExpectedFormAtEveryBoundaryCount() throws {
        let english = try pluralEntries(.english)
        XCTAssertEqual(english.count, 9)
        for language in Self.languages {
            let entries = try pluralEntries(language)
            XCTAssertEqual(Set(entries.keys), Set(english.keys), language.rawValue)
            for (key, entry) in entries {
                let format = try XCTUnwrap(entry["NSStringLocalizedFormatKey"] as? String)
                let placeholders = try XCTUnwrap(BackendSummary.placeholders(in: format), "\(language.rawValue) \(key)")
                for count in Self.edgeCounts {
                    // Every count argument gets the same value, so each plural
                    // variable must show the form for that count.
                    let arguments = placeholders.map { $0 == .object ? BackendSummaryArgument.string("7.8.1") : .int(count) }
                    let context = "\(language.rawValue) \(key) \(count)"
                    let rendered = try XCTUnwrap(render(key, arguments, in: language), context)
                    XCTAssertFalse(rendered.contains("%"), "\(context): \(rendered)")
                    XCTAssertFalse(rendered.contains("#@"), "\(context): \(rendered)")
                    // Foundation uses a zero form, where one exists, for exactly 0.
                    let expected = Self.category(language, count)
                    for (name, value) in entry {
                        guard let forms = value as? [String: String] else { continue }
                        let chosen = count == 0 && forms["zero"] != nil ? "zero" : expected
                        let form = try XCTUnwrap(forms[chosen] ?? forms["other"], "\(context) \(name)")
                        for piece in form.components(separatedBy: "%lld") where !piece.isEmpty {
                            XCTAssertTrue(rendered.contains(piece), "\(context) \(name) \(chosen): \"\(piece)\" not in \"\(rendered)\"")
                        }
                    }
                }
            }
        }
    }
}
