#ifndef FALLOUT_HD_D3D11_SHARED_H_
#define FALLOUT_HD_D3D11_SHARED_H_

#ifdef _WIN32

#include <d3d11.h>

namespace fallout::hd {

// The presenter's device, for the other D3D11 parts of src/hd (map renderer).
// Null while the D3D11 presenter isn't active.
ID3D11Device* presenterDevice();
ID3D11DeviceContext* presenterContext();

// The process's dedicated video memory use and the OS budget for it
// (IDXGIAdapter3::QueryVideoMemoryInfo, local segment), in bytes.
bool presenterVideoMemory(unsigned long long* usage, unsigned long long* budget);

// Compiles one entry point of an HLSL source; logs errors.
ID3DBlob* compileShader(const char* source, size_t length, const char* name, const char* entry, const char* target);

} // namespace fallout::hd

#endif // _WIN32

#endif /* FALLOUT_HD_D3D11_SHARED_H_ */
