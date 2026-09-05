/* ABI oracle for wendydds: prints the dds_topic_descriptor_t layout, the
 * serializer opcodes, the QoS enum values, and the sample-info layout from
 * the real CycloneDDS 0.10.5 headers. run_tests.sh compiles this and the
 * Mojo test suite asserts its comptime constants against the output, so a
 * header change breaks the tests instead of corrupting topics at runtime. */
#include <stdio.h>
#include <stddef.h>
#include <dds/dds.h>
#include <dds/ddsc/dds_opcodes.h>

int main(void) {
  printf("sizeof_descriptor=%zu\n", sizeof(dds_topic_descriptor_t));
  printf("off_m_size=%zu\n", offsetof(dds_topic_descriptor_t, m_size));
  printf("off_m_align=%zu\n", offsetof(dds_topic_descriptor_t, m_align));
  printf("off_m_flagset=%zu\n", offsetof(dds_topic_descriptor_t, m_flagset));
  printf("off_m_nkeys=%zu\n", offsetof(dds_topic_descriptor_t, m_nkeys));
  printf("off_m_typename=%zu\n", offsetof(dds_topic_descriptor_t, m_typename));
  printf("off_m_keys=%zu\n", offsetof(dds_topic_descriptor_t, m_keys));
  printf("off_m_nops=%zu\n", offsetof(dds_topic_descriptor_t, m_nops));
  printf("off_m_ops=%zu\n", offsetof(dds_topic_descriptor_t, m_ops));
  printf("off_m_meta=%zu\n", offsetof(dds_topic_descriptor_t, m_meta));
  printf("off_type_information=%zu\n",
         offsetof(dds_topic_descriptor_t, type_information));
  printf("off_type_mapping=%zu\n",
         offsetof(dds_topic_descriptor_t, type_mapping));
  printf("off_restrict_data_representation=%zu\n",
         offsetof(dds_topic_descriptor_t, restrict_data_representation));
  printf("op_adr=%u\n", (unsigned)DDS_OP_ADR);
  printf("op_rts=%u\n", (unsigned)DDS_OP_RTS);
  printf("op_type_str=%u\n", (unsigned)DDS_OP_TYPE_STR);
  printf("topic_xtypes_metadata=%u\n", (unsigned)DDS_TOPIC_XTYPES_METADATA);
  printf("reliability_reliable=%d\n", (int)DDS_RELIABILITY_RELIABLE);
  printf("durability_volatile=%d\n", (int)DDS_DURABILITY_VOLATILE);
  printf("history_keep_last=%d\n", (int)DDS_HISTORY_KEEP_LAST);
  printf("sizeof_sample_info=%zu\n", sizeof(dds_sample_info_t));
  printf("off_si_valid_data=%zu\n", offsetof(dds_sample_info_t, valid_data));
  return 0;
}
