# set_default_speaker.ps1 - make a playback device the Windows default (all roles).
# Usage:  powershell -ExecutionPolicy Bypass -File set_default_speaker.ps1 "Realtek"
#         (no argument = just list the playback devices)
# Needed because installing VB-Audio Virtual Cable makes "CABLE Input" the
# default speaker, so normal laptop sound would go nowhere.
param([string]$Match = "")

Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

[ComImport, Guid("F8679F50-850A-41CF-9C72-430F290290C8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IPolicyConfig {
    int GetMixFormat(); int GetDeviceFormat(); int ResetDeviceFormat(); int SetDeviceFormat();
    int GetProcessingPeriod(); int SetProcessingPeriod(); int GetShareMode(); int SetShareMode();
    int GetPropertyValue(); int SetPropertyValue();
    [PreserveSig] int SetDefaultEndpoint([MarshalAs(UnmanagedType.LPWStr)] string id, int role);
    int SetEndpointVisibility();
}
[ComImport, Guid("870AF99C-171D-4F9E-AF0D-E63DF40C2BC9")] class PolicyConfigClient {}

[ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceEnumerator {
    [PreserveSig] int EnumAudioEndpoints(int dataFlow, int stateMask, out IMMDeviceCollection devices);
    [PreserveSig] int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice device);
}
[ComImport, Guid("0BD7A1BE-7A1A-44DB-8397-CC5392387B5E"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceCollection {
    [PreserveSig] int GetCount(out int count);
    [PreserveSig] int Item(int index, out IMMDevice device);
}
[ComImport, Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDevice {
    [PreserveSig] int Activate();
    [PreserveSig] int OpenPropertyStore(int access, out IPropertyStore store);
    [PreserveSig] int GetId([MarshalAs(UnmanagedType.LPWStr)] out string id);
}
[ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IPropertyStore {
    [PreserveSig] int GetCount(out int count);
    [PreserveSig] int GetAt(int i, out PROPERTYKEY key);
    [PreserveSig] int GetValue(ref PROPERTYKEY key, out PROPVARIANT value);
}
[StructLayout(LayoutKind.Sequential)] public struct PROPERTYKEY { public Guid fmtid; public int pid; }
[StructLayout(LayoutKind.Sequential)] public struct PROPVARIANT { public short vt; short r1, r2, r3; public IntPtr p; IntPtr p2; }
[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] class MMDeviceEnumerator {}

public static class Audio {
    public static string[] List() {
        var e = (IMMDeviceEnumerator)new MMDeviceEnumerator();
        IMMDeviceCollection c; e.EnumAudioEndpoints(0, 1, out c);   // render, active
        int n; c.GetCount(out n);
        IMMDevice def; e.GetDefaultAudioEndpoint(0, 1, out def); string defId; def.GetId(out defId);
        var result = new string[n];
        for (int i = 0; i < n; i++) {
            IMMDevice d; c.Item(i, out d); string id; d.GetId(out id);
            IPropertyStore ps; d.OpenPropertyStore(0, out ps);
            var key = new PROPERTYKEY { fmtid = new Guid("A45C254E-DF1C-4EFD-8020-67D146A850E0"), pid = 14 };
            PROPVARIANT v; ps.GetValue(ref key, out v);
            string name = Marshal.PtrToStringUni(v.p);
            result[i] = (id == defId ? "*" : " ") + "|" + name + "|" + id;
        }
        return result;
    }
    public static void SetDefault(string id) {
        var pc = (IPolicyConfig)new PolicyConfigClient();
        for (int role = 0; role < 3; role++) Marshal.ThrowExceptionForHR(pc.SetDefaultEndpoint(id, role));
    }
}
"@

$devices = [Audio]::List()
foreach ($d in $devices) { $p = $d.Split("|"); Write-Output ("{0} {1}" -f $p[0], $p[1]) }
if ($Match) {
    $hit = $devices | Where-Object { $_.Split("|")[1] -like "*$Match*" } | Select-Object -First 1
    if (-not $hit) { Write-Output "No playback device matches '$Match'"; exit 1 }
    $p = $hit.Split("|")
    [Audio]::SetDefault($p[2])
    Write-Output ("Default speaker is now: {0}" -f $p[1])
}
