#include "4DPluginAPI.h"
#include "PrivateTypes.h"
#include "EntryPoints.h"
#include "C_TEXT.h"
#include "ARRAY_TEXT.h"
#include "4DPlugin-JSON.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <stdexcept>

static std::map<PA_Unichar*, size_t> strings;
static std::map<PA_Handle, size_t> handles;
struct Ref {
    int owners = 1;
    std::map<CUTF16String, PA_Variable> properties;
};
static std::map<void*, Ref*> refs;
static std::map<PA_Picture, int> pictures;
static bool nullEmpty, failedGetter, failedCommand, failedHandle;
static int failCommandID;
static CUTF16String expected;

static void check(bool valid, const char* message)
{
    if(!valid) {
        std::fprintf(stderr, "ownership assertion: %s\n", message);
        std::exit(1);
    }
}

static CUTF16String utf16(const char* bytes)
{
    CFStringRef s = CFStringCreateWithBytes(kCFAllocatorDefault,
        reinterpret_cast<const UInt8*>(bytes), std::strlen(bytes),
        kCFStringEncodingUTF8, false);
    check(s != nullptr, "valid synthetic UTF8");
    CUTF16String result(CFStringGetLength(s), 0);
    if(!result.empty())
        CFStringGetCharacters(s, CFRangeMake(0, result.size()), &result[0]);
    CFRelease(s);
    return result;
}

static PA_Unistring makeString(const PA_Unichar* text)
{
    size_t units = 0;
    while(text && text[units]) ++units;
    PA_Unistring value = {};
    value.fLength = units;
    if(units || !nullEmpty) {
        value.fString = new PA_Unichar[units + 1];
        if(units) std::memcpy(value.fString, text, units * sizeof(PA_Unichar));
        value.fString[units] = 0;
        PA_Unichar* buffer = value.fString;
        strings[buffer] = units;
    }
    return value;
}

static void textEquals(PA_Unistring value, const CUTF16String& wanted)
{
    check(value.fLength == wanted.size(), "exact UTF16 code-unit count");
    PA_Unichar* buffer = value.fString;
    check(buffer || wanted.empty(), "null only represents empty");
    if(buffer) {
        check(strings.count(buffer), "read live string");
        check(strings.at(buffer) == wanted.size(), "exact allocation capacity");
        check(buffer[wanted.size()] == 0, "terminator within allocation");
        check(CUTF16String(buffer, wanted.size()) == wanted, "exact UTF16 content");
    }
}

static void disposeString(PA_Unistring value)
{
    PA_Unichar* buffer = value.fString;
    if(!buffer) {
        check(value.fLength == 0, "null empty length");
        return;
    }
    check(strings.erase(buffer) == 1, "string disposed exactly once");
    delete[] buffer;
}

static void clear(PA_Variable*);

static void* newRef()
{
    Ref* ref = new Ref;
    refs[ref] = ref;
    return ref;
}

static void retain(void* ref)
{
    if(ref) {
        check(refs.count(ref), "retain live reference");
        ++refs.at(ref)->owners;
    }
}

static void release(void* ref)
{
    if(!ref) return;
    check(refs.count(ref), "release live reference");
    Ref* value = refs.at(ref);
    if(--value->owners == 0) {
        for(auto& property : value->properties) clear(&property.second);
        refs.erase(ref);
        delete value;
    }
}

static PA_Variable copy(PA_Variable value)
{
    if(value.fType == eVK_Unistring)
        value.uValue.fString = makeString(value.uValue.fString.fString);
    else if(value.fType == eVK_Object) retain(value.uValue.fObject);
    else if(value.fType == eVK_Collection) retain(value.uValue.fCollection);
    else if(value.fType == eVK_Picture) {
        PA_Picture p = value.uValue.fPicture;
        if(p) {
            check(pictures.count(p), "retain live picture");
            ++pictures.at(p);
        }
    }
    return value;
}

static void clear(PA_Variable* value)
{
    switch(value->fType) {
        case eVK_Unistring: disposeString(value->uValue.fString); break;
        case eVK_Object: release(value->uValue.fObject); break;
        case eVK_Collection: release(value->uValue.fCollection); break;
        case eVK_Picture: {
            PA_Picture p = value->uValue.fPicture;
            if(p) {
                check(pictures.count(p), "release live picture");
                if(--pictures.at(p) == 0) {
                    pictures.erase(p);
                    delete static_cast<char*>(p);
                }
            }
            break;
        }
        case eVK_ArrayUnicode: {
            PA_Handle h = value->uValue.fArray.fData;
            if(h) {
                check(handles.count(h), "array handle live");
                auto elements = reinterpret_cast<PA_Unistring*>(*h);
                for(int i = 0; i <= value->uValue.fArray.fNbElements; ++i)
                    disposeString(elements[i]);
                PA_DisposeHandle(h);
            }
            break;
        }
        case eVK_Undefined:
        case eVK_Null:
        case eVK_Longint:
        case eVK_Real:
        case eVK_Boolean: break;
        default: check(false, "never clear poisoned output or raw-kind0 sentinel");
    }
    *value = {};
    value->fType = eVK_Undefined;
}

static void host(short selector, EngineBlock* eb)
{
    switch(selector) {
        case EX_CREATE_UNISTRING:
            eb->fUniString1 = makeString(reinterpret_cast<PA_Unichar*>(eb->fHandle));
            break;
        case EX_DISPOSE_UNISTRING: disposeString(eb->fUniString1); break;
        case EX_SET_UNISTRING: {
            auto replacement = makeString(reinterpret_cast<PA_Unichar*>(eb->fHandle));
            disposeString(eb->fUniString1);
            eb->fUniString1 = replacement;
            break;
        }
        case EX_VARIABLE_TO_STRING: {
            auto value = reinterpret_cast<PA_Variable*>(eb->fHandle);
            check(value->fType == eVK_Unistring, "never convert raw-kind0 sentinel");
            eb->fUniString1 = value->uValue.fString;
            textEquals(eb->fUniString1, expected);
            break;
        }
        case EX_CLEAR_VARIABLE: clear(static_cast<PA_Variable*>(eb->fPtr1)); break;
        case EX_CREATE_PICTURE: {
            PA_Picture p = new char(42);
            pictures[p] = 1;
            eb->fPicture = p;
            break;
        }
        case EX_DISPOSE_PICTURE: {
            PA_Variable v = {};
            PA_SetPictureVariable(&v, eb->fPicture);
            clear(&v);
            break;
        }
        case EX_HANDLE_MANAGER: {
            PA_Handle h = eb->fParam1 == 1 ? nullptr : eb->fHandle;
            switch(eb->fParam1) {
                case 1:
                    if(failedHandle) {
                        failedHandle = false;
                        eb->fHandle = nullptr;
                        eb->fError = eER_IncompatibleTypes;
                    } else {
                        h = new char*;
                        *h = static_cast<char*>(std::calloc(1, std::max<sLONG_PTR>(1, eb->fParam2)));
                        check(*h != nullptr, "host handle allocation");
                        handles[h] = eb->fParam2;
                        eb->fHandle = h;
                    }
                    break;
                case 2:
                    check(handles.erase(h) == 1, "handle disposed once");
                    std::free(*h);
                    delete h;
                    break;
                case 3:
                    check(handles.count(h), "resize live handle");
                    *h = static_cast<char*>(std::realloc(*h, eb->fParam2));
                    check(*h != nullptr, "host handle resize");
                    handles[h] = eb->fParam2;
                    break;
                case 4: eb->fParam2 = handles.at(h); break;
                case 5: eb->fParam3 = h ? reinterpret_cast<sLONG_PTR>(*h) : 0; break;
                case 6: break;
                default: check(false, "unexpected handle operation");
            }
            break;
        }
        case EX_GET_OBJ_VALUE: {
            auto output = static_cast<PA_Variable*>(eb->fPtr2);
            if(failedGetter) {
                std::memset(output, 0xa5, sizeof(*output));
                eb->fError = eER_IncompatibleTypes;
            } else {
                auto ref = refs.at(eb->fPtr1);
                CUTF16String key(eb->fUniString1.fString, eb->fUniString1.fLength);
                *output = copy(ref->properties.at(key));
            }
            break;
        }
        case EX_SET_OBJ_VALUE: {
            auto ref = refs.at(eb->fPtr1);
            CUTF16String key(eb->fUniString1.fString, eb->fUniString1.fLength);
            auto found = ref->properties.find(key);
            if(found != ref->properties.end()) clear(&found->second);
            ref->properties[key] = copy(*static_cast<PA_Variable*>(eb->fPtr2));
            break;
        }
        case EX_EXECUTE_COMMAND_BY_ID: {
            auto args = static_cast<PA_Variable**>(eb->fPtr1);
            auto output = static_cast<PA_Variable*>(eb->fPtr2);
            if(failedCommand && eb->fParam1 == failCommandID) {
                std::memset(output, 0xa5, sizeof(*output));
                eb->fError = eER_IncompatibleTypes;
                break;
            }
            switch(eb->fParam1) {
                case 1231: {
                    auto ref = refs.at(args[0]->uValue.fObject);
                    auto key = args[1]->uValue.fString;
                    PA_SetBooleanVariable(output,
                        ref->properties.count(CUTF16String(key.fString, key.fLength)));
                    break;
                }
                case 1225:
                    *output = copy(*args[0]);
                    if(failCommandID == 1217 && !failedCommand) failedHandle = true;
                    break;
                case 1217:
                    if(failCommandID == -1) PA_SetLongintVariable(output, 42);
                    else {
                        output->fType = eVK_Unistring;
                        output->fFiller = 0;
                        output->uValue.fString = makeString(expected.c_str());
                    }
                    break;
                default: check(false, "unexpected command");
            }
            break;
        }
        default: check(false, "unexpected engine selector");
    }
}

static void balanced()
{
    std::printf("strings=%zu handles=%zu refs=%zu pictures=%zu\n",
        strings.size(), handles.size(), refs.size(), pictures.size());
    check(strings.empty(), "no leaked strings (including allocated empties)");
    check(handles.empty(), "no leaked handles");
    check(refs.empty(), "all reference counts balanced");
    check(pictures.empty(), "all picture counts balanced");
}

static void setProperty(void* object, PA_Variable value)
{
    refs.at(object)->properties[utf16("key")] = value;
}

static void getter(const std::string& name, const char* bytes)
{
    void* object = newRef();
    expected = utf16(bytes);
    PA_Variable value = {};
    if(name == "mismatch-string") PA_SetBooleanVariable(&value, true);
    else if(name == "get-bool") PA_SetBooleanVariable(&value, true);
    else if(name == "get-number") PA_SetRealVariable(&value, 42);
    else if(name == "get-object") PA_SetObjectVariable(&value, newRef());
    else if(name == "get-collection") PA_SetCollectionVariable(&value, newRef());
    else {
        auto text = makeString(expected.c_str());
        PA_SetStringVariable(&value, &text);
    }
    setProperty(object, value);
    CUTF8String result(reinterpret_cast<const uint8_t*>("unchanged"));
    failedGetter = name.find("failed-") == 0;
    bool caught = false;
    try {
        if(name == "get-string" || name == "mismatch-string" || name == "failed-string")
            check(ob_get_a(object, L"key", &result), "property exists");
        else if(name == "get-bool" || name == "mismatch-bool" || name == "failed-bool")
            check(ob_get_b(object, L"key") == (name == "get-bool"), "boolean result");
        else if(name == "get-number" || name == "mismatch-number" || name == "failed-number")
            check(ob_get_n(object, L"key") == (name == "get-number" ? 42 : 0), "numeric result");
        else if(name == "get-object" || name == "mismatch-object" || name == "failed-object") {
            void* owned = ob_get_o(object, L"key");
            if(name == "get-object") {
                release(object);
                object = nullptr;
                check(refs.count(owned) && refs.at(owned)->owners == 1, "object handoff survives source");
                PA_DisposeObject(owned);
            } else check(!owned, "mismatch object result");
        } else if(name == "get-collection" || name == "mismatch-collection" || name == "failed-collection") {
            void* owned = ob_get_c(object, L"key");
            if(name == "get-collection") {
                release(object);
                object = nullptr;
                check(refs.count(owned) && refs.at(owned)->owners == 1, "collection handoff survives source");
                PA_DisposeCollection(owned);
            } else check(!owned, "mismatch collection result");
        } else check(false, "unknown getter case");
    } catch(const std::runtime_error&) { caught = true; }
    check(caught == failedGetter, "failed getter explicitly reports before inspecting poison");
    if(name == "get-string")
        check(result == CUTF8String(reinterpret_cast<const uint8_t*>(bytes)), "exact UTF8 getter bytes");
    if(failedGetter)
        check(result == CUTF8String(reinterpret_cast<const uint8_t*>("unchanged")), "failed output preserved");
    release(object);
}

static void setters(const std::string& name, const char* bytes)
{
    void* object = newRef();
    expected = utf16(bytes);
    if(name == "set-narrow") ob_set_s(object, "key", bytes);
    else if(name == "set-wide") ob_set_s(object, L"key", bytes);
    else if(name == "set-utf16") ob_set_a(object, L"key", &expected);
    else if(name == "set-wchar") {
        CFStringRef s = CFStringCreateWithCharacters(kCFAllocatorDefault, expected.c_str(), expected.size());
        std::vector<wchar_t> wide(expected.size() + 1);
        CFIndex used = 0;
        CFStringGetBytes(s, CFRangeMake(0, expected.size()), kCFStringEncodingUTF32LE,
            0, false, reinterpret_cast<UInt8*>(wide.data()), wide.size() * sizeof(wchar_t), &used);
        CFRelease(s);
        ob_set_a(object, L"key", wide.data());
    } else if(name == "set-picture") {
        auto p = PA_CreatePicture(nullptr, 0);
        ob_set_p(object, L"key", p);
        check(pictures.at(p) == 1, "setter transfers argument owner, property retains");
    } else check(false, "unknown setter case");
    if(name != "set-picture")
        textEquals(refs.at(object)->properties.at(utf16("key")).uValue.fString, expected);
    release(object);
}

static void stringify(const std::string& name, const char* bytes)
{
    void* object = newRef();
    expected = utf16(bytes);
    failedCommand = name == "stringify-command-failure" || name == "stringify-duplicate-failure";
    failCommandID = name == "stringify-nonstring" ? -1 :
        name == "stringify-duplicate-failure" ? 1225 :
        name == "stringify-handle-failure" || failedCommand ? 1217 : 0;
    CUTF8String output(reinterpret_cast<const uint8_t*>("unchanged"));
    bool caught = false;
    try { ob_stringify(object, &output); }
    catch(const std::runtime_error&) { caught = true; }
    bool failure = name != "stringify";
    check(caught == failure, "command failure explicitly reports");
    check(output == CUTF8String(reinterpret_cast<const uint8_t*>(failure ? "unchanged" : bytes)),
        "exact stringify bytes / preserved failed output");
    check(refs.at(object)->owners == 1, "synchronous duplicate argument cleared independently");
    release(object);
}

static void arrays()
{
    ARRAY_TEXT source;
    source.setSize(1);
    const char* fixtures[] = {"ASCII", "\xce\xa9", "\xf0\x9f\x98\x80", ""};
    for(auto bytes : fixtures) {
        CUTF16String text = utf16(bytes);
        source.appendUTF16String(&text);
    }
    PA_Variable destination = PA_CreateVariable(eVK_Undefined);
    BytePtr params[] = {reinterpret_cast<BytePtr>(&destination)};
    source.toParamAtIndex(params, 1);
    for(int repeat = 0; repeat < 3; ++repeat) {
        source.toParamAtIndex(params, 1);
        for(int i = 1; i <= 4; ++i)
            textEquals(PA_GetStringInArray(destination, i), utf16(fixtures[i - 1]));
    }
    ARRAY_TEXT copied;
    copied.fromParamAtIndex(params, 1);
    PA_ClearVariable(&destination);
    copied.toParamAtIndex(params, 1);
    textEquals(PA_GetStringInArray(destination, 3), utf16(fixtures[2]));
    auto borrowed = PA_GetStringInArray(destination, 3);
    auto independent = PA_CreateUnistring(borrowed.fString);
    PA_SetStringInArray(destination, 0, &independent);
    PA_ResizeArray(&destination, 0);
    textEquals(PA_GetStringInArray(destination, 0), utf16(fixtures[2]));
    auto rejected = PA_CreateUnistring(independent.fString);
    PA_Variable wrong = PA_CreateVariable(eVK_Longint);
    PA_SetStringInArray(wrong, 0, &rejected);
    PA_SetStringInArray(destination, -1, &rejected);
    PA_SetStringInArray(destination, 1, &rejected);
    auto noData = destination;
    noData.uValue.fArray.fData = nullptr;
    PA_SetStringInArray(noData, 0, &rejected);
    PA_SetStringInArray(destination, 0, nullptr);
    textEquals(rejected, utf16(fixtures[2]));
    PA_DisposeUnistring(&rejected);
    PA_ClearVariable(&destination);
}

static void scalar(const char* bytes, const std::string& name)
{
    expected = utf16(bytes);
    PA_Unistring input = makeString(expected.c_str());
    PA_Unistring output = makeString(nullptr);
    PA_Unistring* slot = &output;
    if(name != "scalar") {
        PA_ObjectRef options = name == "plugin-options" ? newRef() : nullptr;
        if(options) {
            PA_Variable enabled = {};
            PA_SetBooleanVariable(&enabled, true);
            refs.at(options)->properties[utf16("p")] = enabled;
            PA_Variable mismatch = {};
            auto text = makeString(expected.c_str());
            PA_SetStringVariable(&mismatch, &text);
            refs.at(options)->properties[utf16("br")] = mismatch;
        }
        BytePtr params[] = {reinterpret_cast<BytePtr>(&input), reinterpret_cast<BytePtr>(&options)};
        void* data = nullptr;
        FourDPackex(1, params, &data, &slot);
        std::string html = std::strlen(bytes) ? std::string("<p>") + bytes + " </p>" : "";
        textEquals(output, utf16(html.c_str()));
        release(options);
    } else {
        C_TEXT text;
        BytePtr params[] = {reinterpret_cast<BytePtr>(&input)};
        text.fromParamAtIndex(params, 1);
        PA_DisposeUnistring(&input);
        CUTF8String roundtrip;
        text.copyUTF8String(&roundtrip);
        check(roundtrip == CUTF8String(reinterpret_cast<const uint8_t*>(bytes)), "exact UTF8 bytes");
        check(text.getUTF16Length() == expected.size(), "exact wrapper UTF16 units");
        check(text.getUTF16StringPtr()[expected.size()] == 0, "wrapper terminator");
        text.setReturn(reinterpret_cast<sLONG_PTR*>(&slot));
        input = makeString(nullptr);
        text.toParamAtIndex(params, 1);
        textEquals(input, expected);
        PA_Variable alias = PA_CreateVariable(eVK_Undefined);
        auto independent = makeString(expected.c_str());
        PA_SetStringVariable(&alias, &independent);
        check(alias.uValue.fString.fString == independent.fString, "scalar setter shallow alias");
        PA_ClearVariable(&alias);
        PluginBlock block = {};
        block.fResult = &slot;
        PA_ReturnString(&block, const_cast<PA_Unichar*>(expected.c_str()));
        textEquals(output, expected);
    }
    PA_DisposeUnistring(&input);
    PA_DisposeUnistring(&output);
}

int main(int argc, char** argv)
{
    check(argc == 4, "case fixture empty-mode arguments");
    gCall4D = host;
    nullEmpty = std::string(argv[3]) == "null";
    std::string name = argv[1];
    if(name.find("get-") == 0 || name.find("mismatch-") == 0 || name.find("failed-") == 0)
        getter(name, argv[2]);
    else if(name.find("set-") == 0) setters(name, argv[2]);
    else if(name.find("stringify") == 0) stringify(name, argv[2]);
    else if(name == "arrays") arrays();
    else if(name == "scalar" || name == "plugin" || name == "plugin-options") scalar(argv[2], name);
    else if(name == "deferred-array-create") {
        PA_Variable destination = {};
        destination.fType = eVK_Undefined;
        auto original = destination;
        BytePtr params[] = {reinterpret_cast<BytePtr>(&destination)};
        ARRAY_TEXT source;
        source.appendUTF16String(utf16("x").c_str());
        failedHandle = true;
        source.toParamAtIndex(params, 1);
        check(std::memcmp(&destination, &original, sizeof(destination)) == 0,
            "DEFERRED: initial array creation publishes success-shaped null descriptor");
    }
    else check(false, "unknown test");
    balanced();
}
