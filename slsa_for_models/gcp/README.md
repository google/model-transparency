# SLSA for Models on Google Cloud Platform

This project uses [Tekton][tekton] to generate SLSA provenance for ML models on Google Cloud Platform (GCP). It uses 
[Google Kubernetes Engine][gke] (GKE), [Artifact Registry][ar], [Tekton] and [Sigstore].

## Guide

1. To get started, you'll need to have a [GCP Project][gcp]. You will also need to have these CLI tools installed:
   - [`gcloud`][gcloud]
   - [`kubectl`][kubectl]
   - [`tkn`][tkn]
   - [`cosign`][cosign]

2. Enable the needed services:

   ```shell
   gcloud services enable \
     container.googleapis.com \
     artifactregistry.googleapis.com
   ```

3. Create a GKE cluster:

    1. Set the `PROJECT_ID` environment variable from your GCP project:
   
       ```shell
       export PROJECT_ID=<PROJECT_ID>
       ```
   
   2. Set the `CLUSTER_NAME` environment variable to a cluster name of your choice:
   
      ```shell
      export CLUSTER_NAME=<CLUSTER_NAME>
      ```
   
   3. Create a cluster:
   
       ```shell
       gcloud container clusters create $CLUSTER_NAME \
       --enable-autoscaling \
       --min-nodes=1 \
       --max-nodes=3 \
       --scopes=cloud-platform \
       --no-issue-client-certificate \
       --project=$PROJECT_ID \
       --region=us-central1 \
       --machine-type=e2-standard-4 \
       --num-nodes=1 \
       --cluster-version=latest
       ```

4. Install Tekton:

   1. Install Tekton Pipelines:
   
       ```shell
       kubectl apply --filename https://storage.googleapis.com/tekton-releases/pipeline/latest/release.yaml
       ``` 
   
   2. Install Tekton Chains:
   
       ```shell
       kubectl apply --filename https://storage.googleapis.com/tekton-releases/chains/latest/release.yaml
       ```

5. Verify your Tekton installation was successful:

   1. Check that Tekton Pipelines Pods are running in Kubernetes:

       ```shell
       kubectl get pods -n tekton-pipelines
       ```

   2. Check that Tekton Chains Pods are running in Kubernetes:

      ```shell
      kubectl get pods -n tekton-chains 
      ``` 

6. Configure Tekton:

   1. Configure Tekton Pipelines to enable enumerations and alpha features:
   
       ```shell
       kubectl patch cm feature-flags -n tekton-pipelines -p '{"data":{
       "enable-param-enum":"true",
       "enable-api-fields":"alpha" 
       }}'  
       ```

   2. Then restart the Tekton Pipelines controller to ensure it picks up the changes:
      
      ```shell
      kubectl delete pods -n tekton-pipelines -l app=tekton-pipelines-controller
      ``` 
   
   3. Configure Tekton Chains to enable transparency log, set SLSA format and configure storage:
 
      ```shell
      kubectl patch configmap chains-config -n tekton-chains -p='{"data":{
      "transparency.enabled": "true",
      "artifacts.taskrun.format":"slsa/v2alpha2", 
      "artifacts.taskrun.storage": "tekton",
      "artifacts.pipelinerun.format":"slsa/v2alpha2", 
      "artifacts.pipelinerun.storage": "tekton"
      }}'
      ```
   4. Then restart the Tekton Chains controller to ensure it picks up the changes:

      ```shell
      kubectl delete pods -n tekton-chains -l app=tekton-chains-controller 
      ```    

7. Generate an encrypted x509 keypair and save it as a Kubernetes secret:
   
   ```shell
   cosign generate-key-pair k8s://tekton-chains/signing-secrets
   ```
 
8. Apply the `Tasks`:

   ```shell
   kubectl apply -f slsa_for_models/gcp/tasks
   ```

9. Apply the `Pipeline`:

   ```shell
   kubectl apply -f slsa_for_models/gcp/pipeline.yml
   ```
 
10. Create a generic repository in Artifact Registry:

    ```shell
    gcloud artifacts repositories create <REPOSITORY> \
     --location=<LOCATION> \
     --repository-format=generic  
    ```   
   
    For example:
    ```shell
    gcloud artifacts repositories create ml-artifacts \
     --location=us \
     --repository-format=generic  
    ```
   
    If you set a different repository name and [location] from the example above, make sure to modify the `Parameter` 
    named 'model-storage' in the `PipelineRun` with your own values. 

11. Execute the `PipelineRun`:

    ```shell
    kubectl create -f slsa_for_models/gcp/pipelinerun.yml
    ```

12. Observe the `PipelineRun` execution:

    ```shell
    tkn pipelinerun logs --last --follow
    ``` 

13. When the `PipelineRun` succeeds, view its status:

    ```shell
    export PIPELINERUN_NAME=$(tkn pr describe --last -o  jsonpath='{.metadata.name}') 
    kubectl get pipelinerun $PIPELINERUN_NAME -o yaml
    ```

14. View the SLSA attestation:

   ```shell
   cat sig | tr -d '\n' | pbcopy
   pbpaste | jq '.payload | @base64d | fromjson'
   ```

15. View the transparency log entry in the public [Rekor][rekor] instance:

   ```shell
   export TLOG_ENTRY=$(tkn pr describe --last -o jsonpath="{.metadata.annotations.chains\.tekton\.dev/transparency}")
   open $TLOG_ENTRY
   ```
   
16. Retrieve the signature and payload from the `PipelinRun` which are stored as base64-encoded annotations:

   ```shell
   export PIPELINERUN_UID=$(tkn pr describe --last -o  jsonpath='{.metadata.uid}')
   tkn pr describe --last -o jsonpath="{.metadata.annotations.chains\.tekton\.dev/signature-pipelinerun-$PIPELINERUN_UID}" | base64 -d > sig
   ```

17. View the attestation:

   ```shell
   cat sig | tr -d '\n' | pbcopy
   pbpaste | jq '.payload | @base64d | fromjson'
   ```

18. Verify the attestation:

   ```shell
   cosign verify-blob-attestation --key k8s://tekton-chains/signing-secrets --signature sig --type slsaprovenance1 --check-claims=false /dev/null
   ```

## Future Work

### Automate Provenance Verification

Demonstrate how to verify the provenance of the model before deploying and serving the model.

### Kubeflow on Tekton

Provide a Kubeflow Pipeline that can be compiled into the above Tekton Pipeline using [Kubeflow on Tekton][tekton-kubeflow].

### Accelerators

Demonstrate training ML models that require multiple hours for training and require access to accelerators (i.e., GPUs, 
TPUs).

[gcp]: https://cloud.google.com/docs/get-started
[gcloud]: https://cloud.google.com/sdk/docs/install
[kubectl]: https://kubernetes.io/docs/tasks/tools/
[tkn]: https://tekton.dev/docs/cli/
[cosign]: https://docs.sigstore.dev/system_config/installation/
[tekton-kubeflow]: https://www.kubeflow.org/docs/components/pipelines/v1/sdk/pipelines-with-tekton/
[tekton-chains]: https://tekton.dev/docs/chains/
[tekton]: https://tekton.dev/docs/
[rekor]: https://rekor.sigstore.dev
[location]: https://cloud.google.com/artifact-registry/docs/repositories/repo-locations
[gke]: https://cloud.google.com/kubernetes-engine?hl=en
[ar]: https://cloud.google.com/artifact-registry
[sigstore]: https://docs.sigstore.dev 
