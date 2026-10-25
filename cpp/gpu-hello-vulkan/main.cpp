// Vulkan GPU diagnostics: run compute shaders on the device GPU and VERIFY the
// results against a CPU reference. Verification is the point -- a GPU that
// dispatches successfully can still compute wrong numbers, and a benchmark that
// only reports throughput would report it confidently.
#include <vulkan/vulkan.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cmath>
#include <string>
#include <vector>
#include <fstream>
#include <chrono>

#define VKCHECK(x) do { VkResult r__ = (x); if (r__ != VK_SUCCESS) { \
    fprintf(stderr, "vk error %d at %s:%d\n", r__, __FILE__, __LINE__); exit(2); } } while (0)

static std::vector<char> readFile(const char* path) {
    std::ifstream f(path, std::ios::ate | std::ios::binary);
    if (!f.is_open()) { fprintf(stderr, "cannot open %s\n", path); exit(2); }
    size_t sz = (size_t)f.tellg();
    std::vector<char> buf(sz);
    f.seekg(0); f.read(buf.data(), sz);
    return buf;
}

struct Ctx {
    VkInstance inst{};
    VkPhysicalDevice phys{};
    VkDevice dev{};
    VkQueue queue{};
    uint32_t qfam{};
    VkPhysicalDeviceProperties props{};
    VkPhysicalDeviceMemoryProperties memProps{};
};

static uint32_t findMem(Ctx& c, uint32_t typeBits, VkMemoryPropertyFlags want) {
    for (uint32_t i = 0; i < c.memProps.memoryTypeCount; i++)
        if ((typeBits & (1u << i)) &&
            (c.memProps.memoryTypes[i].propertyFlags & want) == want) return i;
    fprintf(stderr, "no suitable memory type\n"); exit(2);
}

struct Buf {
    VkBuffer buf{}; VkDeviceMemory mem{}; void* mapped{}; VkDeviceSize size{};
};

static Buf makeBuf(Ctx& c, VkDeviceSize size) {
    Buf b; b.size = size;
    VkBufferCreateInfo bi{VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO};
    bi.size = size;
    bi.usage = VK_BUFFER_USAGE_STORAGE_BUFFER_BIT;
    bi.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    VKCHECK(vkCreateBuffer(c.dev, &bi, nullptr, &b.buf));
    VkMemoryRequirements req; vkGetBufferMemoryRequirements(c.dev, b.buf, &req);
    VkMemoryAllocateInfo ai{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
    ai.allocationSize = req.size;
    ai.memoryTypeIndex = findMem(c, req.memoryTypeBits,
        VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
    VKCHECK(vkAllocateMemory(c.dev, &ai, nullptr, &b.mem));
    VKCHECK(vkBindBufferMemory(c.dev, b.buf, b.mem, 0));
    VKCHECK(vkMapMemory(c.dev, b.mem, 0, size, 0, &b.mapped));
    return b;
}

struct Kernel {
    VkShaderModule mod{}; VkDescriptorSetLayout dsl{}; VkPipelineLayout pl{};
    VkPipeline pipe{}; VkDescriptorPool pool{}; VkDescriptorSet ds{};
};

static Kernel makeKernel(Ctx& c, const char* spv) {
    Kernel k;
    auto code = readFile(spv);
    VkShaderModuleCreateInfo smi{VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO};
    smi.codeSize = code.size();
    smi.pCode = reinterpret_cast<const uint32_t*>(code.data());
    VKCHECK(vkCreateShaderModule(c.dev, &smi, nullptr, &k.mod));

    VkDescriptorSetLayoutBinding b[3]{};
    for (int i = 0; i < 3; i++) {
        b[i].binding = i; b[i].descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
        b[i].descriptorCount = 1; b[i].stageFlags = VK_SHADER_STAGE_COMPUTE_BIT;
    }
    VkDescriptorSetLayoutCreateInfo dli{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO};
    dli.bindingCount = 3; dli.pBindings = b;
    VKCHECK(vkCreateDescriptorSetLayout(c.dev, &dli, nullptr, &k.dsl));

    VkPushConstantRange pc{VK_SHADER_STAGE_COMPUTE_BIT, 0, sizeof(uint32_t)};
    VkPipelineLayoutCreateInfo pli{VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO};
    pli.setLayoutCount = 1; pli.pSetLayouts = &k.dsl;
    pli.pushConstantRangeCount = 1; pli.pPushConstantRanges = &pc;
    VKCHECK(vkCreatePipelineLayout(c.dev, &pli, nullptr, &k.pl));

    VkComputePipelineCreateInfo cpi{VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO};
    cpi.stage.sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO;
    cpi.stage.stage = VK_SHADER_STAGE_COMPUTE_BIT;
    cpi.stage.module = k.mod; cpi.stage.pName = "main";
    cpi.layout = k.pl;
    VKCHECK(vkCreateComputePipelines(c.dev, VK_NULL_HANDLE, 1, &cpi, nullptr, &k.pipe));

    VkDescriptorPoolSize ps{VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 3};
    VkDescriptorPoolCreateInfo dpi{VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO};
    dpi.maxSets = 1; dpi.poolSizeCount = 1; dpi.pPoolSizes = &ps;
    VKCHECK(vkCreateDescriptorPool(c.dev, &dpi, nullptr, &k.pool));
    VkDescriptorSetAllocateInfo dsa{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO};
    dsa.descriptorPool = k.pool; dsa.descriptorSetCount = 1; dsa.pSetLayouts = &k.dsl;
    VKCHECK(vkAllocateDescriptorSets(c.dev, &dsa, &k.ds));
    return k;
}

static void bindBufs(Ctx& c, Kernel& k, Buf& a, Buf& b, Buf& out) {
    // NOTE: assigned element-by-element rather than with a braced initialiser list.
    // Every file in a template is rendered through Go text/template, which treats
    // two consecutive opening braces as the start of an action. A nested braced
    // initialiser would therefore break `wendy init` at render time.
    VkDescriptorBufferInfo bi[3];
    bi[0] = { a.buf,   0, a.size   };
    bi[1] = { b.buf,   0, b.size   };
    bi[2] = { out.buf, 0, out.size };
    VkWriteDescriptorSet w[3]{};
    for (int i = 0; i < 3; i++) {
        w[i].sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
        w[i].dstSet = k.ds; w[i].dstBinding = i; w[i].descriptorCount = 1;
        w[i].descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
        w[i].pBufferInfo = &bi[i];
    }
    vkUpdateDescriptorSets(c.dev, 3, w, 0, nullptr);
}

static double dispatch(Ctx& c, Kernel& k, uint32_t n, uint32_t gx, uint32_t gy) {
    VkCommandPoolCreateInfo cpi{VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO};
    cpi.queueFamilyIndex = c.qfam;
    VkCommandPool pool; VKCHECK(vkCreateCommandPool(c.dev, &cpi, nullptr, &pool));
    VkCommandBufferAllocateInfo cbi{VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO};
    cbi.commandPool = pool; cbi.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY; cbi.commandBufferCount = 1;
    VkCommandBuffer cb; VKCHECK(vkAllocateCommandBuffers(c.dev, &cbi, &cb));

    VkCommandBufferBeginInfo bb{VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
    bb.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
    VKCHECK(vkBeginCommandBuffer(cb, &bb));
    vkCmdBindPipeline(cb, VK_PIPELINE_BIND_POINT_COMPUTE, k.pipe);
    vkCmdBindDescriptorSets(cb, VK_PIPELINE_BIND_POINT_COMPUTE, k.pl, 0, 1, &k.ds, 0, nullptr);
    vkCmdPushConstants(cb, k.pl, VK_SHADER_STAGE_COMPUTE_BIT, 0, sizeof(uint32_t), &n);
    vkCmdDispatch(cb, gx, gy, 1);
    VKCHECK(vkEndCommandBuffer(cb));

    auto t0 = std::chrono::steady_clock::now();
    VkSubmitInfo si{VK_STRUCTURE_TYPE_SUBMIT_INFO};
    si.commandBufferCount = 1; si.pCommandBuffers = &cb;
    VKCHECK(vkQueueSubmit(c.queue, 1, &si, VK_NULL_HANDLE));
    VKCHECK(vkQueueWaitIdle(c.queue));
    auto t1 = std::chrono::steady_clock::now();

    vkDestroyCommandPool(c.dev, pool, nullptr);
    return std::chrono::duration<double>(t1 - t0).count();
}

int main() {
    Ctx c;
    VkApplicationInfo app{VK_STRUCTURE_TYPE_APPLICATION_INFO};
    app.pApplicationName = "gpu-hello-vulkan"; app.apiVersion = VK_API_VERSION_1_1;
    VkInstanceCreateInfo ii{VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO};
    ii.pApplicationInfo = &app;
    VKCHECK(vkCreateInstance(&ii, nullptr, &c.inst));

    uint32_t nd = 0; vkEnumeratePhysicalDevices(c.inst, &nd, nullptr);
    if (!nd) { printf("{\"error\":\"no Vulkan device found\"}\n"); return 1; }
    std::vector<VkPhysicalDevice> devs(nd);
    vkEnumeratePhysicalDevices(c.inst, &nd, devs.data());
    c.phys = devs[0];
    vkGetPhysicalDeviceProperties(c.phys, &c.props);
    vkGetPhysicalDeviceMemoryProperties(c.phys, &c.memProps);

    uint32_t nq = 0; vkGetPhysicalDeviceQueueFamilyProperties(c.phys, &nq, nullptr);
    std::vector<VkQueueFamilyProperties> qs(nq);
    vkGetPhysicalDeviceQueueFamilyProperties(c.phys, &nq, qs.data());
    c.qfam = UINT32_MAX;
    for (uint32_t i = 0; i < nq; i++) if (qs[i].queueFlags & VK_QUEUE_COMPUTE_BIT) { c.qfam = i; break; }
    if (c.qfam == UINT32_MAX) { printf("{\"error\":\"no compute queue\"}\n"); return 1; }

    float pri = 1.0f;
    VkDeviceQueueCreateInfo qi{VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO};
    qi.queueFamilyIndex = c.qfam; qi.queueCount = 1; qi.pQueuePriorities = &pri;
    VkDeviceCreateInfo di{VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO};
    di.queueCreateInfoCount = 1; di.pQueueCreateInfos = &qi;
    VKCHECK(vkCreateDevice(c.phys, &di, nullptr, &c.dev));
    vkGetDeviceQueue(c.dev, c.qfam, 0, &c.queue);

    // ---- vector add -------------------------------------------------------
    const uint32_t N = 1u << 20;                 // 1,048,576 elements
    Buf a = makeBuf(c, N * sizeof(float));
    Buf b = makeBuf(c, N * sizeof(float));
    Buf o = makeBuf(c, N * sizeof(float));
    float* ap = (float*)a.mapped; float* bp = (float*)b.mapped; float* op = (float*)o.mapped;
    for (uint32_t i = 0; i < N; i++) { ap[i] = (float)(i % 1000) * 0.5f; bp[i] = (float)(i % 7) * 1.25f; }
    memset(op, 0, N * sizeof(float));

    Kernel kv = makeKernel(c, "/app/vecadd.spv");
    bindBufs(c, kv, a, b, o);
    double tv = dispatch(c, kv, N, (N + 255) / 256, 1);

    uint32_t vbad = 0; double vmaxerr = 0;
    for (uint32_t i = 0; i < N; i++) {
        double want = (double)ap[i] + (double)bp[i];
        double err = fabs(want - (double)op[i]);
        if (err > 1e-4) { vbad++; if (err > vmaxerr) vmaxerr = err; }
    }
    double vgbs = (3.0 * N * sizeof(float)) / tv / 1e9;

    // ---- matmul -----------------------------------------------------------
    const uint32_t M = 256;                      // 256x256
    Buf ma = makeBuf(c, M * M * sizeof(float));
    Buf mb = makeBuf(c, M * M * sizeof(float));
    Buf mo = makeBuf(c, M * M * sizeof(float));
    float* map_ = (float*)ma.mapped; float* mbp = (float*)mb.mapped; float* mop = (float*)mo.mapped;
    for (uint32_t i = 0; i < M * M; i++) { map_[i] = (float)((i * 13) % 17) * 0.25f; mbp[i] = (float)((i * 7) % 11) * 0.5f; }
    memset(mop, 0, M * M * sizeof(float));

    Kernel km = makeKernel(c, "/app/matmul.spv");
    bindBufs(c, km, ma, mb, mo);
    double tm = dispatch(c, km, M, (M + 15) / 16, (M + 15) / 16);

    uint32_t mbad = 0; double mmaxerr = 0;
    for (uint32_t r = 0; r < M; r++) {
        for (uint32_t col = 0; col < M; col++) {
            double want = 0;
            for (uint32_t k = 0; k < M; k++) want += (double)map_[r * M + k] * (double)mbp[k * M + col];
            double err = fabs(want - (double)mop[r * M + col]);
            double tol = fabs(want) * 1e-4 + 1e-3;
            if (err > tol) { mbad++; if (err > mmaxerr) mmaxerr = err; }
        }
    }
    double mgflops = (2.0 * (double)M * M * M) / tm / 1e9;

    const char* vendor = "unknown";
    switch (c.props.vendorID) {
        case 0x5143: vendor = "Qualcomm"; break;  case 0x13B5: vendor = "ARM"; break;
        case 0x10DE: vendor = "NVIDIA";   break;  case 0x1002: vendor = "AMD"; break;
        case 0x8086: vendor = "Intel";    break;
    }
    const char* types[] = {"OTHER","INTEGRATED_GPU","DISCRETE_GPU","VIRTUAL_GPU","CPU"};

    printf("{\n");
    printf("  \"device\": \"%s\",\n", c.props.deviceName);
    printf("  \"vendor\": \"%s\",\n", vendor);
    printf("  \"type\": \"%s\",\n", types[c.props.deviceType <= 4 ? c.props.deviceType : 0]);
    printf("  \"api_version\": \"%u.%u.%u\",\n", VK_VERSION_MAJOR(c.props.apiVersion),
           VK_VERSION_MINOR(c.props.apiVersion), VK_VERSION_PATCH(c.props.apiVersion));
    printf("  \"vecadd\": { \"elements\": %u, \"ms\": %.3f, \"gb_per_s\": %.2f, \"mismatches\": %u, \"max_err\": %.3e, \"pass\": %s },\n",
           N, tv * 1000.0, vgbs, vbad, vmaxerr, vbad == 0 ? "true" : "false");
    printf("  \"matmul\": { \"size\": %u, \"ms\": %.3f, \"gflops\": %.2f, \"mismatches\": %u, \"max_err\": %.3e, \"pass\": %s },\n",
           M, tm * 1000.0, mgflops, mbad, mmaxerr, mbad == 0 ? "true" : "false");
    printf("  \"pass\": %s\n", (vbad == 0 && mbad == 0) ? "true" : "false");
    printf("}\n");
    return (vbad == 0 && mbad == 0) ? 0 : 1;
}
